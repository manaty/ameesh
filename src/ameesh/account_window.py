# SPDX-License-Identifier: AGPL-3.0-only
"""#3779 — suivre la fenêtre courte du fournisseur, par compte de forfait.

Codex (forfait ChatGPT) n'expose qu'un bucket hebdomadaire dans
`account/rateLimits/read` (`codex`, 10080 min). Le fournisseur applique en plus
un **bucket de rafale / concurrence non documenté, au niveau du compte** :
plusieurs sessions du même compte reçoivent 429 sur la première requête d'un
tour alors que la jauge hebdomadaire est très loin de son plafond, sans
`retry-after` ni en-tête `x-ratelimit` lisible (openai/codex#41702,
holon-run/holon#3369). Les agents réessaient alors chacun de leur côté et
entretiennent la rafale.

Ce module rend cette limite **visible et partagée** :

* `record_rate_limit` journalise un 429 dans `account_rate_events`
  (migration 0491), clé `(harness, account)` — le compte logique, le même nom
  sur tous les hôtes du réservoir ;
* `state` relit les 429 des 15 dernières minutes et calcule un **cooldown de
  compte** : 4 min après le premier refus, doublé à chaque refus supplémentaire
  de la fenêtre, plafonné à 15 min. Tous les agents du compte s'arrêtent, pas
  seulement le fautif ;
* `record_start` journalise le début d'un tour (`account_turn_starts`) ;
* `reason` refuse un nouveau tour quand le cooldown court, quand l'écart
  minimal depuis le dernier début n'est pas écoulé, ou quand le nombre de
  tours `running` du compte atteint `max_concurrent` (2 pour Codex ; l'écart
  de 30 s est le levier principal : cinq débuts en 90 s suffisaient à voir
  des 429 même avec un seul tour actif). La lecture des tours actifs est celle
  du registre central (`agent_registry.status = 'running'`), donc cross-hôtes ;
* `rows`/`format` nourrissent `ameesh accounts list` : la « jauge de fenêtre
  courte » demandée par le témoin du lot.

Aucun secret ne transite : seuls le nom du compte, celui de l'agent, l'hôte et
le texte d'erreur du fournisseur (borné) sont écrits. Un backend de stockage
sans ce domaine (exécuteur médié d'avant le contrat) est ignoré : la garde
retombe sur le comportement d'avant, jamais sur une erreur de tour.
"""
from __future__ import annotations

import time

from . import storage

#: Harnais dont le fournisseur applique un bucket court non exposé. Un harnais
#: absent n'est jamais ralenti par ce module.
SHORT_WINDOW_HARNESSES = ("codex",)

#: Défauts d'admission par harnais, surchargeables par le profil de compte
#: (`max_concurrent`, `min_start_gap_s`). Codex : 2 tours actifs au plus, et
#: 30 s entre deux débuts de tour — cinq débuts en 90 s suffisaient à produire
#: des 429 même avec un seul tour actif (#41702, relevé #3779).
DEFAULT_MAX_ACTIVE = {"codex": 2}
DEFAULT_MIN_START_GAP_S = {"codex": 30.0}

#: Fenêtre d'observation des 429 (15 min).
BURST_WINDOW_S = 900.0
#: Cooldown de base après un 429, et plafond de l'escalade.
COOLDOWN_BASE_S = 240.0
COOLDOWN_MAX_S = 900.0
#: Longueur maximale du texte d'erreur conservé (jamais une invite).
DETAIL_MAX = 500

#: Genres acceptés par la table (miroir du CHECK de la migration 0491).
RATE_LIMITED = "rate_limited"


def _log(message: str) -> None:
    from .runner import log_async
    log_async(message)


def _api(db):
    """Le domaine partagé, ou None (base absente, backend médié sans contrat)."""
    if db is None:
        return None
    try:
        return getattr(storage.of(db), "account_windows", None)
    except Exception:  # noqa: BLE001 - lecture facultative, jamais fatale
        return None


def enabled(harness: str | None) -> bool:
    """Ce harnais suit-il la fenêtre courte ?"""
    return (harness or "") in SHORT_WINDOW_HARNESSES


def max_active(harness: str, profile=None) -> int:
    """Le plafond de tours simultanés du compte (0 = pas de garde).

    Le profil de compte (`accounts.Profile`) peut le régler (`max_concurrent`,
    `None` = défaut du harnais ; 0 = garde désactivée).
    """
    value = getattr(profile, "max_concurrent", None) if profile is not None else None
    if value is not None:
        return int(value)
    return int(DEFAULT_MAX_ACTIVE.get(harness or "", 0) or 0)


def min_start_gap(harness: str, profile=None) -> float:
    """L'écart minimal entre deux débuts de tour du compte, en secondes.

    Surchargeable par le profil (`min_start_gap_s` ; 0 = pas d'écart).
    """
    value = getattr(profile, "min_start_gap_s", None) if profile is not None else None
    if value is not None:
        return float(value)
    return float(DEFAULT_MIN_START_GAP_S.get(harness or "", 0.0) or 0.0)


def _blank(harness: str, account: str, profile=None) -> dict:
    return {
        "harness": harness,
        "account": account,
        "active": [],
        "active_count": 0,
        "max_active": max_active(harness, profile),
        "min_start_gap_s": min_start_gap(harness, profile),
        "last_start_at": None,
        "next_start_at": None,
        "recent_429": 0,
        "last_429_at": None,
        "last_agent": None,
        "last_host": None,
        "last_detail": None,
        "cooldown_s": 0.0,
        "cooldown_until": None,
        "source": "unavailable",
    }


def cooldown_for(count: int) -> float:
    """La durée du cooldown après `count` refus dans la fenêtre (escalade)."""
    if count <= 0:
        return 0.0
    return min(COOLDOWN_MAX_S, COOLDOWN_BASE_S * (2 ** max(0, min(count, 8) - 1)))


def state(db, harness: str, account: str, now: float | None = None,
          exclude_agent: str | None = None, profile=None) -> dict:
    """L'état de fenêtre courte d'un compte logique.

    `exclude_agent` retire l'appelant du compte des tours actifs : un agent qui
    se voit déjà `running` ne se bloque pas lui-même (choix d'avant tour).
    `profile` porte les seuils du compte (`max_concurrent`,
    `min_start_gap_s`) ; absent = défauts du harnais.
    """
    now = time.time() if now is None else float(now)
    out = _blank(harness, account, profile)
    if not enabled(harness):
        out["source"] = "not-tracked"
        return out
    api = _api(db)
    if api is None:
        return out
    try:
        rows = api.active(harness, account) or []
        out["active"] = [
            {"name": row.get("name"), "host": row.get("host"),
             "status_since_ts": row.get("status_since_ts")}
            for row in rows if row.get("name") != exclude_agent
        ]
        out["active_count"] = len(out["active"])
        recent = api.recent(harness, account, BURST_WINDOW_S) or {}
        out["recent_429"] = int(recent.get("count") or 0)
        out["last_429_at"] = recent.get("last_at_ts")
        out["last_agent"] = recent.get("last_agent")
        out["last_host"] = recent.get("last_host")
        out["last_detail"] = recent.get("last_detail")
        out["cooldown_s"] = cooldown_for(out["recent_429"])
        if out["last_429_at"] is not None and out["cooldown_s"] > 0:
            out["cooldown_until"] = float(out["last_429_at"]) + out["cooldown_s"]
        # #3779 : l'écart entre deux débuts se lit sur le journal des débuts,
        # que le statut `running` ne suffit pas à donner (un tour fini change
        # `status_since`).
        last_start = api.last_start(harness, account) or {}
        out["last_start_at"] = last_start.get("last_at_ts")
        if out["last_start_at"] is not None and out["min_start_gap_s"] > 0:
            out["next_start_at"] = (float(out["last_start_at"])
                                    + out["min_start_gap_s"])
        out["source"] = "journal"
    except Exception as exc:  # noqa: BLE001 - état illisible : pas de garde, jamais d'échec
        _log("[account_window] %s/%s state unreadable (%s)"
             % (harness, account, exc))
    return out


def _moment(ts: float | None) -> str:
    if not ts:
        return "?"
    return time.strftime("%H:%M", time.localtime(float(ts)))


def _delay(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "%ds" % int(round(seconds))
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return "%d min" % minutes
    hours, minutes = divmod(minutes, 60)
    return "%d h %02d" % (hours, minutes) if minutes else "%d h" % hours


def reason(db, harness: str, account: str, now: float | None = None,
           exclude_agent: str | None = None, profile=None) -> str:
    """La raison de pause de fenêtre courte, ou '' (aucune garde).

    Le cooldown et l'écart de début RETARDENT le prochain tour ; ils ne
    coupent jamais un tour en cours (`reason` n'est lu qu'avant `begin_turn`).
    """
    now = time.time() if now is None else float(now)
    if not enabled(harness) or not account:
        return ""
    st = state(db, harness, account, now=now, exclude_agent=exclude_agent,
               profile=profile)
    if st.get("cooldown_until") and float(st["cooldown_until"]) > now:
        return ("account %s: short-window cooldown until %s (in %s; %d provider "
                "429 in %s, shared burst bucket, #3779)" % (
                    account, _moment(st["cooldown_until"]),
                    _delay(float(st["cooldown_until"]) - now),
                    st["recent_429"], _delay(BURST_WINDOW_S)))
    if st.get("next_start_at") and float(st["next_start_at"]) > now:
        return ("account %s: next turn in %s (start gap %.0fs between turns, "
                "#3779; last start %s)" % (
                    account, _delay(float(st["next_start_at"]) - now),
                    float(st.get("min_start_gap_s") or 0.0),
                    _moment(st.get("last_start_at"))))
    if st.get("max_active") and st["active_count"] >= st["max_active"]:
        others = ", ".join(str(row.get("name") or "?") for row in st["active"][:3])
        return ("account %s: %d/%d concurrent turns (provider burst bucket shared "
                "by sessions, #3779%s)" % (
                    account, st["active_count"], st["max_active"],
                    "; running: %s" % others if others else ""))
    return ""


def record_rate_limit(db, harness: str, account: str | None, host: str,
                      agent: str, detail: str | None = None) -> None:
    """Journalise un 429 du fournisseur pour le compte logique.

    Best-effort : un journal indisponible ne change pas l'issue du tour ; le
    cooldown local de l'agent (#1270) reste la première garde.
    """
    if not enabled(harness) or not account:
        return
    api = _api(db)
    if api is None:
        return
    texte = " ".join(str(detail or "").split())[:DETAIL_MAX] or None
    try:
        api.record(harness, account, host or "", agent or "", RATE_LIMITED, texte)
    except Exception as exc:  # noqa: BLE001 - le journal ne casse jamais un tour
        _log("[%s] short-window 429 not recorded (%s)" % (agent, exc))


def record_start(db, harness: str, account: str | None, host: str,
                 agent: str) -> None:
    """Journalise le début d'un tour du compte logique (#3779).

    Best-effort : le début reste valable même si le journal est indisponible ;
    l'écart entre deux débuts est alors seulement celui du `status_since`.
    """
    if not enabled(harness) or not account:
        return
    api = _api(db)
    if api is None:
        return
    try:
        api.start(harness, account, host or "", agent or "")
    except Exception as exc:  # noqa: BLE001 - le journal ne casse jamais un tour
        _log("[%s] short-window turn start not recorded (%s)" % (agent, exc))


def rows(db, accounts_rows: list[dict], now: float | None = None,
         profiles=None) -> list[dict]:
    """Une ligne de fenêtre courte par compte à forfait, pour `accounts list`.

    `profiles` : `{harness: [accounts.Profile]}` (la configuration de l'hôte),
    pour lire les seuils surchargés (`max_concurrent`, `min_start_gap_s`).
    """
    now = time.time() if now is None else float(now)
    lookup: dict[tuple[str, str], object] = {}
    for harness, items in (profiles or {}).items():
        for profile in items:
            lookup[(harness, profile.name)] = profile
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in accounts_rows or ():
        harness = str(row.get("harness") or "")
        account = str(row.get("account") or "")
        if not enabled(harness) or (harness, account) in seen:
            continue
        seen.add((harness, account))
        st = state(db, harness, account, now=now,
                   profile=lookup.get((harness, account)))
        out.append({
            "key": "%s-short-window" % harness,
            "harness": harness,
            "account": account,
            "active": st["active_count"],
            "max_active": st["max_active"],
            "min_start_gap_s": st["min_start_gap_s"],
            "last_start_at": st["last_start_at"],
            "next_start_at": st["next_start_at"],
            "recent_429": st["recent_429"],
            "last_429_at": st["last_429_at"],
            "last_agent": st["last_agent"],
            "last_host": st["last_host"],
            "cooldown_until": st["cooldown_until"],
            "cooldown_s": st["cooldown_s"],
            "source": st["source"],
        })
    return out


def format_rows(rows: list[dict], now: float | None = None) -> list[str]:
    """Les lignes lisibles de la jauge courte (sans préfixe d'en-tête)."""
    now = time.time() if now is None else float(now)
    lines: list[str] = []
    for row in rows or ():
        bits = ["%d/%d turn active" % (int(row.get("active") or 0),
                                       int(row.get("max_active") or 0))]
        gap = float(row.get("min_start_gap_s") or 0.0)
        if gap > 0:
            bits.append("start gap %.0fs" % gap)
        next_start = row.get("next_start_at")
        if next_start and float(next_start) > now:
            bits.append("next start in %s" % _delay(float(next_start) - now))
        bits.append("%d 429 in %s" % (int(row.get("recent_429") or 0),
                                      _delay(BURST_WINDOW_S)))
        until = row.get("cooldown_until")
        if until and float(until) > now:
            bits.append("cooldown %s until %s" % (
                _delay(float(until) - now), _moment(float(until))))
        elif until:
            bits.append("cooldown expired at %s" % _moment(float(until)))
        last = row.get("last_429_at")
        if last:
            bits.append("last %s%s" % (
                _moment(float(last)),
                " by %s" % row["last_agent"] if row.get("last_agent") else ""))
        lines.append("  %-9s %-14s %-18s %s" % (
            row.get("harness"), row.get("account"),
            row.get("key") or "%s-short-window" % row.get("harness"),
            "; ".join(bits)))
    return lines
