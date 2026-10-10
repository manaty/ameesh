# SPDX-License-Identifier: AGPL-3.0-only
"""Correction tracée des lignes fausses du grand livre `turn_costs` (lot L95).

  ameesh cost correct [--since 30d] [--json]     essai : rien n'est écrit
  ameesh cost correct --apply [--since 30d]       applique, en une transaction

Trois défauts d'avant L60 et L71, constatés le 2026-10-10 :

* **coût cumulé d'une session reprise** : le premier tour d'une session
  reprise sans repère connu valait toute l'histoire de la session (Claude
  `total_cost_usd`, Codex usage cumulé du fil) — 67 $ pour un tour de
  1 500 jetons de sortie. Claude : le coût du tour est ré-estimé par ses
  jetons (barème du modèle s'il est connu, sinon coût par jeton relu des
  autres tours de la même session). Codex : le total du fil avant le tour se
  lit dans son journal de session (`rollout-…-<fil>.jsonl`) quand il est sur
  cette machine ; sinon, la ligne est écartée (coût du tour inconnu) ;
* **modèle « inconnu »** : facturé au tarif le plus cher de la famille alors
  que le harnais lançait son modèle par défaut (L60). Le tour est re-facturé
  au modèle par défaut du descripteur du harnais ; pour Codex, au modèle lu
  dans le journal du fil ;
* **doublons** : la réparation du marqueur comptable réécrivait la ligne d'un
  tour (avant la clé `spend_key` de L60). Mêmes agent, session, tour, coût et
  jetons, à moins de dix minutes d'écart : les suivantes sont écartées.

Rien n'est supprimé (migration 0043) : une ligne écartée garde tout et porte
`void_reason` (les sommes l'ignorent) ; chaque correction copie d'abord la
ligne entière dans `turn_cost_corrections`. Une ligne déjà écartée n'est plus
touchée, et une passe rejouée ne retrouve rien à corriger : les lignes
corrigées ne remplissent plus les conditions.

Le mode par défaut est l'**essai** : lecture seule, il dit ce qui serait fait.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from typing import Callable, Iterable

from . import config as config_mod
from . import cost as cost_mod
from . import db as db_mod
from . import harnesses
from . import storage

#: doublon : même tour réécrit à moins de dix minutes d'écart
DUP_WINDOW_S = 600.0
#: coût cumulé : le tour vaut plus de CUMUL_FACTOR fois son estimation par les
#: jetons, et au moins CUMUL_MIN_USD de plus (en deçà, la création de cache,
#: que le grand livre ne garde pas, suffit à l'expliquer)
CUMUL_FACTOR = 5.0
CUMUL_MIN_USD = 2.0
#: fil Codex repris : créé plus de douze heures avant la ligne
CODEX_RESUME_AGE_S = 12 * 3600.0
#: modèles « inconnus » des lignes d'avant L60
UNKNOWN_MODELS = ("", "inconnu")

_TOKENS = ("input_tokens", "cached_input_tokens", "output_tokens")


class CorrectionError(RuntimeError):
    """Correction impossible (base sans la migration 0043, par exemple)."""


# --------------------------------------------------------------------------
# lecture du journal d'un fil Codex
# --------------------------------------------------------------------------

def codex_total_before(path: str, cumulative: list) -> list | None:
    """Le total du fil **avant** le tour dont le total final est `cumulative`.

    Le journal de session Codex publie des `token_count` cumulés et un
    `task_started` à chaque tour. Le tour de la ligne est celui dont un
    `token_count` porte exactement le total de la ligne ; son total d'avant
    est le dernier `token_count` qui précède son `task_started`. None si le
    journal ne le dit pas ; `[0, 0, 0]` si le tour est le premier du fil.
    """
    last_total = None
    before_turn = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if "task_started" not in line and "token_count" not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                payload = event.get("payload") if isinstance(event, dict) else None
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "task_started":
                    before_turn = last_total or [0, 0, 0]
                elif payload.get("type") == "token_count":
                    total = (payload.get("info") or {}).get("total_token_usage")
                    if not isinstance(total, dict):
                        continue
                    last_total = [int(total.get("input_tokens", 0)),
                                  int(total.get("cached_input_tokens", 0)),
                                  int(total.get("output_tokens", 0))]
                    if last_total == list(cumulative):
                        return before_turn if before_turn is not None else [0, 0, 0]
    except (OSError, ValueError, TypeError):
        return None
    return None


def thread_created_ts(thread: str | None) -> float | None:
    """L'instant de création d'un fil d'identifiant UUID v7 (Codex), ou None."""
    try:
        value = uuid.UUID(str(thread))
    except (TypeError, ValueError):
        return None
    if value.version != 7:
        return None
    return int(value.hex[:12], 16) / 1000.0


# --------------------------------------------------------------------------
# le plan de correction (pur : aucune écriture)
# --------------------------------------------------------------------------

def _price(prices: dict, harness: str, model: str) -> list:
    if model and model in prices:
        return list(prices[model])
    return cost_mod.max_price_of(prices, harness)


def _usd(harness: str, tokens: list, triplet: list) -> float:
    """Coût de jetons `[entrée, cache relu, sortie]` au barème (USD/M).

    Codex compte le cache DANS l'entrée (même règle que `cost.turn_usage`)."""
    inp, cached, out = (int(t or 0) for t in tokens)
    if harness == "codex":
        inp = max(inp - cached, 0)
    return (inp * triplet[0] + cached * triplet[1] + out * triplet[2]) / 1e6


def _tokens(row: dict) -> list:
    return [int(row.get(k) or 0) for k in _TOKENS]


def default_model(harness: str) -> str:
    """Le modèle par défaut déclaré par le descripteur du harnais, ou ''."""
    descriptor = harnesses.get(harness)
    return descriptor.default_of("model") if descriptor else ""


def plan(rows: Iterable[dict], *, prices: dict,
         default_model_of: Callable[[str], str] = default_model,
         codex_rollout: Callable[[str], str | None] | None = None,
         codex_model: Callable[[str], str] | None = None) -> list[dict]:
    """Les corrections à faire, dans l'ordre des lignes. Rien n'est écrit.

    Chaque correction : `{id, agent, harness, session, kind, reason, set,
    before, after}` ; `set` est ce que `TurnCosts.correct` écrira, `before` et
    `after` résument coût, modèle et jetons pour l'affichage.
    """
    rows = [dict(r) for r in rows if not r.get("void_reason")]
    rows.sort(key=lambda r: (float(r.get("recorded_ts") or 0.0), int(r["id"])))
    fixes: dict = {}

    def fix(row: dict, kind: str, reason: str, **values) -> None:
        entry = fixes.setdefault(int(row["id"]), {
            "id": int(row["id"]), "agent": row["agent"], "harness": row["harness"],
            "session": row.get("session"), "kinds": [], "reasons": [], "set": {},
            "before": {"usd": round(float(row.get("usd") or 0.0), 6),
                       "model": row.get("model"), "tokens": _tokens(row)}})
        entry["kinds"].append(kind)
        entry["reasons"].append(reason)
        entry["set"].update(values)

    # 1. doublons : la première ligne reste, les suivantes sont écartées
    voided: set = set()
    groups: dict = {}
    for row in rows:
        if sum(_tokens(row)) <= 0:
            continue
        key = (row["agent"], row["harness"], row.get("session") or "", row.get("turn") or "",
               round(float(row.get("usd") or 0.0), 6), tuple(_tokens(row)),
               row.get("cum_usd"), row.get("cum_input_tokens"))
        groups.setdefault(key, []).append(row)
    for group in groups.values():
        first = group[0]
        for row in group[1:]:
            if float(row["recorded_ts"]) - float(first["recorded_ts"]) <= DUP_WINDOW_S:
                voided.add(int(row["id"]))
                fix(row, "doublon", "doublon de la ligne #%d (marqueur comptable rejoué, "
                    "avant la clé spend_key de L60)" % int(first["id"]),
                    void_reason="doublon de #%d" % int(first["id"]))
            else:
                first = row
    live = [r for r in rows if int(r["id"]) not in voided]

    # 2. coût cumulé d'une session reprise : premier relevé de chaque session
    sessions: dict = {}
    for row in live:
        if row.get("session") and row["harness"] in ("claude", "codex"):
            sessions.setdefault((row["harness"], row["session"]), []).append(row)
    repriced: dict = {}            # id -> jetons du tour après correction
    for (harness, session), items in sessions.items():
        first = items[0]
        if harness == "claude":
            _claude_cumul(first, items[1:], prices, fix, repriced)
        else:
            _codex_cumul(first, prices, fix, repriced, codex_rollout, codex_model)

    # 3. modèle « inconnu » : modèle par défaut du descripteur, ou journal Codex
    for row in live:
        if int(row["id"]) in voided or (row.get("model") or "") not in UNKNOWN_MODELS:
            continue
        if "void_reason" in fixes.get(int(row["id"]), {}).get("set", {}):
            continue                        # écartée : son prix ne compte plus
        model = ""
        source = ""
        if row["harness"] == "codex" and codex_model and row.get("session"):
            model = codex_model(row["session"]) or ""
            source = "lu dans le journal du fil Codex, sinon sa configuration"
        if not model:
            model = default_model_of(row["harness"]) or ""
            source = "modèle par défaut du descripteur %s, lancé faute de réglage" % (
                row["harness"])
        if not model:
            continue
        tokens = repriced.get(int(row["id"]), _tokens(row))
        current = fixes.get(int(row["id"]), {}).get("set", {}).get("usd")
        if row["harness"] == "claude" and current is not None:
            fix(row, "modele", "modèle « inconnu » → %s (%s)" % (model, source), model=model)
            continue
        usd = round(_usd(row["harness"], tokens, _price(prices, row["harness"], model)), 6)
        old = float(row.get("usd") or 0.0) if current is None else float(current)
        values = {"model": model}
        if abs(usd - old) >= 1e-6:
            values["usd"] = usd
        fix(row, "modele", ("modèle « inconnu » facturé au plus cher de la famille → %s (%s)"
                            if "usd" in values else "modèle « inconnu » → %s (%s), coût "
                            "inchangé") % (model, source), **values)

    out = []
    for ident in sorted(fixes):
        entry = fixes[ident]
        before = entry["before"]
        after = {"usd": entry["set"].get("usd", before["usd"]),
                 "model": entry["set"].get("model", before["model"]),
                 "tokens": [entry["set"].get(k, before["tokens"][i])
                            for i, k in enumerate(_TOKENS)],
                 "void": "void_reason" in entry["set"]}
        out.append({"id": ident, "agent": entry["agent"], "harness": entry["harness"],
                    "session": entry["session"], "kind": "+".join(entry["kinds"]),
                    "reason": " ; ".join(entry["reasons"]), "set": entry["set"],
                    "before": before, "after": after})
    return out


def _claude_cumul(first: dict, others: list, prices: dict, fix, repriced: dict) -> None:
    """Claude : le premier relevé de la session vaut-il tout son cumul ?"""
    cum = first.get("cum_usd")
    usd = float(first.get("usd") or 0.0)
    if cum is None or usd <= 0 or abs(usd - float(cum)) > 1e-6:
        return
    tokens = _tokens(first)
    model = first.get("model") or ""
    if model in prices:
        estimate = _usd("claude", tokens, prices[model])
        how = "barème %s" % model
    else:
        spent = sum(float(r.get("usd") or 0.0) for r in others)
        read = sum(int(r.get("input_tokens") or 0) + int(r.get("cached_input_tokens") or 0)
                   for r in others)
        if spent > 0 and read > 0:
            estimate = (tokens[0] + tokens[1]) * spent / read
            how = "coût par jeton relu des %d autres tours de la session" % len(others)
        else:
            estimate = _usd("claude", tokens, cost_mod.max_price_of(prices, "claude"))
            how = "tarif le plus cher connu"
    if usd <= CUMUL_FACTOR * estimate or usd - estimate <= CUMUL_MIN_USD:
        return
    fix(first, "cumul", "coût cumulé de la session reprise (%.2f $) compté pour un tour ; "
        "ré-estimé par ses jetons (%s)" % (usd, how), usd=round(estimate, 6))


def _codex_cumul(first: dict, prices: dict, fix, repriced: dict,
                 codex_rollout, codex_model) -> None:
    """Codex : le premier relevé du fil porte-t-il tout l'usage du fil ?"""
    if first.get("cum_input_tokens") is None:
        return
    tokens = _tokens(first)
    cumulative = [int(first.get("cum_input_tokens") or 0),
                  int(first.get("cum_cached_input_tokens") or 0),
                  int(first.get("cum_output_tokens") or 0)]
    if tokens != cumulative or sum(tokens) <= 0:
        return                              # la ligne est déjà une différence
    path = codex_rollout(first["session"]) if codex_rollout else None
    before = codex_total_before(path, cumulative) if path else None
    if before is not None:
        if not any(before):
            return                          # premier tour du fil : la ligne est juste
        delta = [max(c - b, 0) for c, b in zip(cumulative, before)]
        model = first.get("model") or ""
        if model in UNKNOWN_MODELS and codex_model:
            model = codex_model(first["session"]) or ""
        usd = round(_usd("codex", delta, _price(prices, "codex", model)), 6)
        repriced[int(first["id"])] = delta
        fix(first, "cumul", "usage cumulé du fil repris (%s jetons d'entrée) compté pour un "
            "tour ; total d'avant le tour lu dans le journal du fil" % cumulative[0],
            usd=usd, input_tokens=delta[0], cached_input_tokens=delta[1],
            output_tokens=delta[2])
        return
    created = thread_created_ts(first["session"])
    if created is None or float(first["recorded_ts"]) - created < CODEX_RESUME_AGE_S:
        return
    fix(first, "cumul", "usage cumulé d'un fil repris (créé %.0f h avant) compté pour un "
        "tour ; journal du fil absent de cette machine : coût du tour inconnu, ligne "
        "écartée" % ((float(first["recorded_ts"]) - created) / 3600.0),
        void_reason="cumul d'un fil Codex repris, coût du tour inconnu")


# --------------------------------------------------------------------------
# résumé et rendu
# --------------------------------------------------------------------------

def summarize(corrections: list[dict]) -> dict:
    """Par agent : coût avant, après (lignes écartées à 0), lignes touchées."""
    agents: dict = {}
    for c in corrections:
        slot = agents.setdefault(c["agent"], {"agent": c["agent"], "harness": c["harness"],
                                              "rows": 0, "voided": 0, "usd_before": 0.0,
                                              "usd_after": 0.0})
        slot["rows"] += 1
        slot["usd_before"] += c["before"]["usd"]
        if c["after"]["void"]:
            slot["voided"] += 1
        else:
            slot["usd_after"] += float(c["after"]["usd"])
    for slot in agents.values():
        slot["usd_before"] = round(slot["usd_before"], 6)
        slot["usd_after"] = round(slot["usd_after"], 6)
    rows = [agents[k] for k in sorted(agents)]
    return {"agents": rows, "rows": len(corrections),
            "voided": sum(r["voided"] for r in rows),
            "usd_before": round(sum(r["usd_before"] for r in rows), 6),
            "usd_after": round(sum(r["usd_after"] for r in rows), 6)}


def format_text(corrections: list[dict], applied: int | None) -> str:
    lines = []
    for c in corrections:
        after = ("écartée" if c["after"]["void"] else "%.4f $" % c["after"]["usd"])
        lines.append("#%-6d %-14s %-9s %-14s %10.4f $ → %s" % (
            c["id"], c["agent"], c["harness"], c["kind"], c["before"]["usd"], after))
        lines.append("        ↳ %s" % c["reason"])
    total = summarize(corrections)
    lines.append("")
    lines.append("%-14s %-9s %6s %8s %12s %12s" % ("agent", "harnais", "lignes", "écartées",
                                                    "avant ($)", "après ($)"))
    for a in total["agents"]:
        lines.append("%-14s %-9s %6d %8d %12.4f %12.4f" % (
            a["agent"], a["harness"], a["rows"], a["voided"], a["usd_before"], a["usd_after"]))
    lines.append("%-14s %-9s %6d %8d %12.4f %12.4f" % (
        "total", "", total["rows"], total["voided"], total["usd_before"], total["usd_after"]))
    lines.append("")
    if applied is None:
        lines.append("ESSAI : rien n'a été écrit. Relancer avec --apply pour corriger "
                     "(copie de chaque ligne dans turn_cost_corrections, aucune suppression).")
    else:
        lines.append("%d ligne(s) corrigée(s) ; copies dans turn_cost_corrections." % applied)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

def run(db, *, apply: bool = False, since_ts: float | None = None,
        book: cost_mod.CostBook | None = None, actor: str = "",
        prices_path: str | None = None) -> tuple[list[dict], int | None]:
    """Calcule le plan et, avec `apply`, l'écrit. Rend (corrections, appliquées)."""
    book = book or cost_mod.CostBook(state_dir="", db=None)
    prices = cost_mod.load_prices(prices_path or book.prices_path)
    rows = storage.of(db).turn_costs.ledger(since_ts=since_ts)
    corrections = plan(rows, prices=prices, codex_rollout=book.codex_rollout,
                       codex_model=book.codex_thread_model)
    if not apply:
        return corrections, None
    run_id = "l95-%s-%s" % (time.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:6])
    try:
        done = storage.of(db).turn_costs.correct(
            run_id=run_id, actor=actor or "human:%s" % (os.environ.get("USER") or "?"),
            corrections=[{"id": c["id"], "kind": c["kind"], "reason": c["reason"],
                          "set": c["set"]} for c in corrections])
    except db_mod.DbError as exc:
        if "turn_cost_corrections" in str(exc) or "void_reason" in str(exc):
            raise CorrectionError("la migration 0043 n'est pas passée : lancer "
                                  "`ameesh migrate` d'abord") from None
        raise
    return corrections, done


_SINCE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([hdj])\s*$", re.I)


def parse_since(text: str | None, now: float) -> float | None:
    if not text:
        return None
    match = _SINCE_RE.match(text)
    if not match:
        raise CorrectionError("--since illisible : %r (30d, 48h)" % text)
    unit = 3600.0 if match.group(2).lower() == "h" else 86400.0
    return now - float(match.group(1)) * unit


def add_parser(cost_sub, func) -> None:
    p = cost_sub.add_parser(
        "correct", help="corrige les lignes fausses du grand livre (essai par défaut)",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true",
                   help="écrit les corrections (sinon : essai, lecture seule)")
    p.add_argument("--since", default=None, help="seulement les lignes récentes (30d, 48h)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=func)


def main(cfg, args) -> int:
    """`ameesh cost correct` : connexion sans exiger le dernier schéma, pour
    que l'essai lise une base d'avant 0043 ; `--apply` l'exige."""
    try:
        since_ts = parse_since(getattr(args, "since", None), time.time())
        db = db_mod.connect(cfg)
        try:
            book = cost_mod.CostBook(state_dir=cfg.state_dir, db=None,
                                     codex_homes=_codex_homes(cfg))
            corrections, applied = run(db, apply=bool(args.apply), since_ts=since_ts,
                                       book=book)
        finally:
            db.close()
    except CorrectionError as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 2
    except db_mod.Unavailable as exc:
        print("erreur : base injoignable : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.DbError as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps({"schema": "ameesh-ledger-corrections/1",
                          "applied": applied, "corrections": corrections,
                          "summary": summarize(corrections)},
                         ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_text(corrections, applied))
    return 0


def _codex_homes(cfg) -> list:
    from . import accounts as accounts_mod
    return accounts_mod.homes(cfg, "codex")


if __name__ == "__main__":
    print("usage : ameesh cost correct [--apply] [--since 30d] [--json]", file=sys.stderr)
    sys.exit(2)
