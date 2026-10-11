# SPDX-License-Identifier: AGPL-3.0-only
"""Durée estimée de chaque lot, durée réelle mesurée, écarts (lot L157).

  ameesh work add --title … --estimate 2h [--estimate-source S]
  ameesh work plan <id> --estimate 90m [--estimate-source S]
  ameesh mail send <agent> "…" --new-lot "titre" --estimate 2h
  ameesh work estimates [--app P] [--json]

Demande du propriétaire (2026-10-11) : « ameesh orchestre des harnais et doit
prévoir la roadmap complète des projets ; il doit toujours estimer le temps
que prennent les tâches, dès la conception des lots ; ensuite l'auditeur
audite le processus d'estimation (qui dépend de l'organisation) et l'ajuste ».

* **Estimation** : une durée en minutes (`90`, `90m`, `2h`, `1h30`, `1,5h`,
  `2d` = 48 h écoulées), sa source et son auteur. Elle est **obligatoire pour
  un lot créé par un agent** (un nom du registre) : sans elle, la création
  est refusée ; pour un humain, c'est un avertissement. Chaque estimation
  posée laisse une ligne d'historique (`work_item_estimates`) ; celle de la
  création est aussi dans la ligne de journal « création ».
* **Réel** : début = premier passage en build (ou qa), ou premier tour de
  l'agent assigné sur le lot (noté par l'exécuteur), le premier des deux ;
  fin = jalon `merged`. La durée est du temps ÉCOULÉ (horloge murale), comme
  l'estimation.
* **Écarts** (`work estimates`) : sur les lots livrés, le ratio réel/estimé
  (médiane, p80) par type de lot et par auteur d'estimation. L'estimation
  comparée est celle EN VIGUEUR AU DÉBUT du travail ; une estimation posée
  après le début est comptée à part (`tardive`). C'est la matière de
  l'auditeur (docs/AUDITEUR.md), qui ajuste le processus d'estimation.

Le JSON de `work estimates` suit le schéma `ameesh-estimates/1`.
"""
from __future__ import annotations

import math
import re
import time

from . import storage
from . import work as work_mod

SCHEMA = "ameesh-estimates/1"
#: lots livrés lus au plus pour les écarts (les plus récents)
HISTORY_LIMIT = 1000
#: au-delà, une estimation est sans doute une faute de frappe (60 jours)
MAX_MINUTES = 60 * 24 * 60
#: ratio réel/estimé au-delà duquel un lot en cours est signalé dépassé
OVERRUN = 1.0

_UNITS = {"m": 1, "min": 1, "mn": 1, "h": 60, "d": 1440, "j": 1440}
_RE_SIMPLE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(m|min|mn|h|d|j)?$")
_RE_HM = re.compile(r"^(\d+)\s*h\s*(\d{1,2})\s*(?:m|min|mn)?$")


class EstimateError(work_mod.WorkError):
    """Estimation refusée (illisible, nulle, manquante pour un agent)."""


def parse(text) -> int:
    """Une durée estimée en minutes (entier > 0) : `90` (minutes), `90m`,
    `2h`, `1h30`, `1,5h`, `2d`/`2j` (jours de 24 h écoulées)."""
    if isinstance(text, bool):
        raise EstimateError("estimation illisible : %r" % (text,))
    if isinstance(text, (int, float)):
        raw = str(text)
    else:
        raw = str(text or "").strip().lower().replace(" ", "")
    match = _RE_HM.match(raw)
    if match:
        minutes = int(match.group(1)) * 60 + int(match.group(2))
    else:
        match = _RE_SIMPLE.match(raw)
        if not match:
            raise EstimateError("estimation illisible : %r (90m, 2h, 1h30, 1,5h, 2d — ou des "
                                "minutes)" % (text,))
        minutes = float(match.group(1).replace(",", ".")) * _UNITS[match.group(2) or "m"]
        minutes = int(round(minutes))
    if minutes <= 0:
        raise EstimateError("estimation nulle : %r (une durée positive)" % (text,))
    if minutes > MAX_MINUTES:
        raise EstimateError("estimation de %s : au-delà de 60 jours, découpez le lot"
                            % work_mod.span(minutes * 60))
    return minutes


def label(minutes) -> str:
    """« 45 min », « 2 h », « 1 h 30 », « 2 j 4 h »."""
    return work_mod.span(float(minutes or 0) * 60)


def compact(minutes) -> str:
    """Forme courte pour une colonne : « 45m », « 2h », « 1h30 », « 3j »."""
    minutes = int(round(float(minutes or 0)))
    if minutes < 60:
        return "%dm" % minutes
    if minutes < 1440:
        hours, rest = divmod(minutes, 60)
        return "%dh%02d" % (hours, rest) if rest else "%dh" % hours
    days, rest = divmod(minutes, 1440)
    return "%dj%dh" % (days, rest // 60) if rest >= 60 else "%dj" % days


def ratio_text(ratio) -> str:
    """« ×1,33 »."""
    return "×%s" % ("%.2f" % ratio).rstrip("0").rstrip(".").replace(".", ",")


# --------------------------------------------------------------------------
# qui doit estimer
# --------------------------------------------------------------------------

def is_agent(db, actor: str | None) -> bool:
    """Un agent, pour la règle d'estimation : un nom du registre des agents
    (ni `human:…`, ni « inconnu », ni un nom absent du registre)."""
    from . import registry

    name = (actor or "").strip()
    if not name or ":" in name or name == work_mod.UNKNOWN_ACTOR:
        return False
    try:
        return registry.get(db, name) is not None
    except Exception:  # noqa: BLE001 - registre illisible : la règle n'empêche rien
        return False


REQUIRED = ("estimation obligatoire : un lot créé par un agent (%s) porte sa durée estimée "
            "dès sa création — %s (90m, 2h, 1h30, 1,5h, 2d)")
MISSING = ("lot sans durée estimée : posez-la (ameesh work plan %s --estimate 2h) — tout lot "
           "porte une durée estimée dès sa création")


def check(db, actor: str | None, minutes, *, option: str = "--estimate 2h") -> bool:
    """La règle (L157) : sans estimation, un AGENT est refusé
    (EstimateError) ; pour un humain, rend True (avertir : `missing(id)`).
    False si une estimation est donnée. `option` : ce qu'il faut ajouter à
    la commande refusée."""
    if minutes:
        return False
    if is_agent(db, actor):
        raise EstimateError(REQUIRED % (actor, "ajoutez %s" % option))
    return True


def missing(item_id) -> str:
    """L'avertissement d'un lot créé sans estimation par un humain."""
    return MISSING % item_id


# --------------------------------------------------------------------------
# écrire
# --------------------------------------------------------------------------

def set_estimate(db, item_id: int, estimate, *, source: str | None = None,
                 actor: str = "") -> dict:
    """Pose (ou révise) la durée estimée d'un lot non terminé. Rend le lot.
    L'historique (`work_item_estimates`) garde chaque estimation : qui,
    quand, combien, d'où ; l'écart se mesure avec celle du début."""
    minutes = parse(estimate)
    item = work_mod.get(db, item_id)
    if item is None:
        raise EstimateError("lot %s introuvable" % item_id)
    if item["state"] in work_mod.MERGED_STATES or item["state"] == "closed":
        raise EstimateError("lot %s déjà %s : son estimation est figée (l'écart se mesure "
                            "avec l'estimation du début)" % (item_id, item["state"]))
    source = " ".join(str(source or "").split()) or None
    row = storage.of(db).work.set_estimate(item_id, minutes, source=source, actor=actor)
    if row is None:
        raise EstimateError("lot %s terminé entre-temps : estimation non posée" % item_id)
    return row


def mark_started(db, agent: str, lots) -> list[int]:
    """L'exécuteur : un tour de `agent` commence sur ces lots. Ceux qui lui
    sont assignés, ouverts et sans début mesuré, reçoivent leur début."""
    ids = sorted({int(str(lot).strip()) for lot in lots or ()
                  if str(lot or "").strip().isdigit()})
    if not ids:
        return []
    return storage.of(db).work.mark_started(
        agent, ids, "début mesuré : premier tour de %s sur le lot" % agent)


# --------------------------------------------------------------------------
# lire : un lot
# --------------------------------------------------------------------------

def _f(value):
    return None if value is None else float(value)


def view(item: dict, *, merged_ts=None, now: float | None = None) -> dict:
    """L'estimation et le réel d'un lot, pour `work show`, `progress`, la
    frise, `projects` et les issues :

    `{minutes, source, by, at_ts, started_ts, finished_ts, actual_minutes,
    elapsed_minutes, ratio, gap_minutes, overrun, label}` — `actual_minutes`
    une fois livré (fusion − début), `elapsed_minutes` en cours ; `ratio` =
    réel (ou écoulé) / estimé ; `overrun` : en cours et au-delà de
    l'estimation. Les valeurs inconnues sont None."""
    now = time.time() if now is None else float(now)
    minutes = item.get("estimate_minutes")
    minutes = int(minutes) if minutes else None
    started = _f(item.get("started_ts"))
    finished = _f(merged_ts if merged_ts is not None else item.get("merged_ts"))
    state = item.get("state") or ""
    done = finished is not None or state in work_mod.MERGED_STATES
    actual = elapsed = None
    if started is not None and finished is not None:
        actual = max(0.0, finished - started) / 60.0
    elif started is not None and not done and state != "closed":
        elapsed = max(0.0, now - started) / 60.0
    measured = actual if actual is not None else elapsed
    ratio = round(measured / minutes, 3) if minutes and measured is not None else None
    gap = round(actual - minutes, 1) if minutes and actual is not None else None
    out = {"minutes": minutes, "source": item.get("estimate_source") or None,
           "by": item.get("estimate_by") or None,
           "at_ts": _f(item.get("estimate_ts")), "started_ts": started,
           "finished_ts": finished,
           "actual_minutes": round(actual, 1) if actual is not None else None,
           "elapsed_minutes": round(elapsed, 1) if elapsed is not None else None,
           "ratio": ratio, "gap_minutes": gap,
           "overrun": bool(elapsed is not None and ratio is not None and ratio > OVERRUN)}
    out["label"] = describe(out)
    return out


def describe(v: dict) -> str:
    """« estimé 2 h · réel 2 h 40 (+40 min, ×1,33) », « estimé 2 h · en cours
    depuis 1 h 10 », « sans estimation · réel 3 h »."""
    parts = ["estimé %s" % label(v["minutes"]) if v.get("minutes") else "sans estimation"]
    if v.get("actual_minutes") is not None:
        text = "réel %s" % label(v["actual_minutes"])
        if v.get("gap_minutes") is not None:
            gap = v["gap_minutes"]
            text += " (%s%s, %s)" % ("+" if gap >= 0 else "−", label(abs(gap)),
                                    ratio_text(v["ratio"]))
        parts.append(text)
    elif v.get("elapsed_minutes") is not None:
        text = "en cours depuis %s" % label(v["elapsed_minutes"])
        if v.get("overrun"):
            text += " (DÉPASSÉ, %s)" % ratio_text(v["ratio"])
        parts.append(text)
    elif v.get("finished_ts") is not None:
        parts.append("réel inconnu (début non mesuré)")
    return " · ".join(parts)


def cell(v: dict | None) -> str:
    """La colonne courte de `ameesh projects` : « 1h10/2h », « 2h » (pas
    commencé), « 2h40/2h! » (dépassé), « — » (sans estimation)."""
    if not v:
        return "—"
    measured = v.get("actual_minutes")
    if measured is None:
        measured = v.get("elapsed_minutes")
    if not v.get("minutes"):
        return "%s/?" % compact(measured) if measured is not None else "—"
    if measured is None:
        return compact(v["minutes"])
    return "%s/%s%s" % (compact(measured), compact(v["minutes"]),
                        "!" if v.get("ratio") and v["ratio"] > OVERRUN else "")


# --------------------------------------------------------------------------
# lire : les écarts (`ameesh work estimates`)
# --------------------------------------------------------------------------

def quantile(values, q: float) -> float | None:
    """Quantile par interpolation linéaire (médiane : q = 0,5)."""
    data = sorted(float(v) for v in values)
    if not data:
        return None
    pos = (len(data) - 1) * float(q)
    lo, hi = math.floor(pos), math.ceil(pos)
    return round(data[lo] + (data[hi] - data[lo]) * (pos - lo), 3)


def _group(rows: list[dict], key) -> list[dict]:
    groups: dict = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    out = []
    for name, members in groups.items():
        ratios = [m["ratio"] for m in members]
        out.append({"key": name, "lots": len(members),
                    "median": quantile(ratios, 0.5), "p80": quantile(ratios, 0.8),
                    "estimated_minutes": sum(m["estimate_minutes"] for m in members),
                    "actual_minutes": round(sum(m["actual_minutes"] for m in members), 1),
                    "late": sum(1 for m in members if m["late"])})
    out.sort(key=lambda g: (-g["lots"], str(g["key"])))
    return out


def build(rows: list[dict], *, app: str | None = None, now: float | None = None) -> dict:
    """Le rapport `ameesh-estimates/1` depuis les lots livrés
    (`work.estimate_history`)."""
    now = time.time() if now is None else float(now)
    lots, no_estimate, no_start = [], [], []
    for row in rows:
        if row.get("started_ts") is None:
            no_start.append(int(row["id"]))
            continue
        if not row.get("estimate_minutes"):
            no_estimate.append(int(row["id"]))
            continue
        actual = max(0.0, float(row["merged_ts"]) - float(row["started_ts"])) / 60.0
        estimate = int(row["estimate_minutes"])
        lots.append({
            "id": int(row["id"]), "title": row.get("title") or "",
            "type": row.get("type") or "?", "app": row.get("app") or row.get("workstream")
            or None, "estimate_by": row.get("estimate_by") or work_mod.UNKNOWN_ACTOR,
            "estimate_source": row.get("estimate_source") or None,
            "estimate_minutes": estimate, "actual_minutes": round(actual, 1),
            "ratio": round(actual / estimate, 3), "late": bool(row.get("late")),
            "revisions": int(row.get("revisions") or 0),
            "merged_ts": round(float(row["merged_ts"]), 3)})
    on_time = [l for l in lots if not l["late"]]
    overall = _group([dict(l, all="*") for l in on_time], lambda r: r["all"])
    return {
        "schema": SCHEMA, "generated_ts": round(now, 3), "app": app or None,
        "delivered": len(rows),
        # les lots estimés dès le début : la base de l'ajustement
        "overall": overall[0] if overall else {"key": "*", "lots": 0, "median": None,
                                                "p80": None, "estimated_minutes": 0,
                                                "actual_minutes": 0, "late": 0},
        "by_type": _group(on_time, lambda r: r["type"]),
        "by_author": _group(on_time, lambda r: r["estimate_by"]),
        # estimées après le début du travail : comptées à part
        "late": _group(lots, lambda r: "tardive" if r["late"] else "au début")
        if any(l["late"] for l in lots) else [],
        "revised": sum(1 for l in lots if l["revisions"] > 1),
        "excluded": {"no_estimate": len(no_estimate), "no_start": len(no_start),
                     "no_estimate_ids": no_estimate[:50], "no_start_ids": no_start[:50]},
        "lots": lots,
    }


def report(db, *, app: str | None = None, limit: int = HISTORY_LIMIT,
           now: float | None = None) -> dict:
    rows = storage.of(db).work.estimate_history(app=app, limit=limit)
    return build(rows, app=app, now=now)


def _ratio_cell(value) -> str:
    return ratio_text(value) if value is not None else "—"


def format_report(rep: dict) -> str:
    out = []
    o = rep["overall"]
    out.append("écarts réel/estimé%s — %d lot(s) livré(s), %d estimé(s) dès le début" % (
        " (projet %s)" % rep["app"] if rep.get("app") else "", rep["delivered"], o["lots"]))
    if o["lots"]:
        out.append("ensemble : médiane %s · p80 %s · estimé %s, réel %s" % (
            _ratio_cell(o["median"]), _ratio_cell(o["p80"]), label(o["estimated_minutes"]),
            label(o["actual_minutes"])))
    for title, rows in (("PAR TYPE DE LOT", rep["by_type"]),
                        ("PAR AUTEUR D'ESTIMATION", rep["by_author"])):
        if not rows:
            continue
        out.append("")
        out.append(title)
        out.append("  %-24s %5s %9s %7s %11s %11s" % ("", "LOTS", "MÉDIANE", "P80", "ESTIMÉ",
                                                       "RÉEL"))
        for g in rows:
            out.append("  %-24s %5d %9s %7s %11s %11s" % (
                str(g["key"])[:24], g["lots"], _ratio_cell(g["median"]),
                _ratio_cell(g["p80"]), label(g["estimated_minutes"]),
                label(g["actual_minutes"])))
    late = next((g for g in rep["late"] if g["key"] == "tardive"), None)
    ex = rep["excluded"]
    notes = []
    if late:
        notes.append("%d estimé(s) après le début (médiane %s), hors calcul" % (
            late["lots"], _ratio_cell(late["median"])))
    if rep["revised"]:
        notes.append("%d lot(s) ré-estimé(s)" % rep["revised"])
    if ex["no_estimate"]:
        notes.append("%d livré(s) sans estimation" % ex["no_estimate"])
    if ex["no_start"]:
        notes.append("%d sans début mesuré" % ex["no_start"])
    if notes:
        out.append("")
        out.append("à part : " + " · ".join(notes))
    if not o["lots"]:
        out.append("")
        out.append("aucun lot livré avec une estimation posée avant le début : rien à "
                   "comparer encore")
    return "\n".join(out)
