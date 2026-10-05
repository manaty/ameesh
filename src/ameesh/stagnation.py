# SPDX-License-Identifier: AGPL-3.0-only
"""Lots stagnants et « en attente de quoi, de qui » — module UNIQUE (L26 + L29).

Appelé par l'alerte `stale_lot` (`ameesh alerts`, L26), par `ameesh progress`
et par `ameesh work list/show` (L29) : les trois vues disent la même chose.

**Activité** d'un lot : modification de sa ligne, transition ou note,
jalon, action liée, message lié (`agent_mailbox.work_item_id`). Un lot
**stagnant** est un lot ni fusionné (`merged`, `promoted`) ni fermé
(`closed`, L29) sans activité depuis un seuil (défaut 6 h ; `--stale-after`,
`AMEESH_STALE_AFTER`).

**Attente** (`waiting`) : `{what, who, label}`, déduite de l'état, des jalons
(L10, lus par `progress._jalons_de_lot`) et des actions, jamais inventée
(`who` est None quand ameesh ne sait pas qui). Ordre :

1. une action du lot qui attend un humain — approbation (`proposed`) ou
   réconciliation (`unknown`) — passe avant tout (`merge-approval`,
   `merge-outcome` pour la fusion `git-merge`, `decision` sinon) ;
2. `waiting_human` → `decision` (du responsable de la fiche du lot) ;
3. un verdict `blocked` POSTÉRIEUR au dernier gel → `fix` (l'auteur) ;
4. puis selon l'état : `start`, `build`, `unblock`, `verdict`, `merge`.

`waiting_for` (forme d'origine de L26, champ du même nom de `ameesh-alert/1`)
en est le résumé en un mot : `verdict`, `correction`, `fusion`,
`decision_humaine`, ou None.
"""
from __future__ import annotations

import os
import re
import time
from typing import Iterable

from . import storage

#: seuil par défaut d'un lot stagnant (secondes)
DEFAULT_THRESHOLD_S = 6 * 3600.0
STALE_ENV = "AMEESH_STALE_AFTER"
#: ce qu'un lot peut attendre, forme résumée de L26 (valeurs de `waiting_for`)
WAITING_FOR = ("verdict", "correction", "fusion", "decision_humaine")
#: ce qu'un lot peut attendre, forme détaillée (`waiting.what`)
WHATS = ("start", "build", "verdict", "fix", "merge-approval", "merge", "merge-outcome",
         "decision", "unblock")
_LEGACY = {"verdict": "verdict", "fix": "correction", "merge": "fusion",
           "decision": "decision_humaine", "merge-approval": "decision_humaine",
           "merge-outcome": "decision_humaine"}
#: états d'un lot qui n'attend plus rien
DONE_STATES = ("merged", "promoted", "closed")
#: états d'action qui attendent un humain
_HUMAN_ACTION_STATES = ("proposed", "unknown")
MERGE_CONNECTOR = "git-merge"

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdj]?)\s*$", re.I)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "j": 86400}


class StaleError(ValueError):
    """Seuil illisible."""


def parse_duration(text: str) -> float:
    """« 90m », « 6h », « 2d »/« 2j », « 3600 » → secondes (strictement positif)."""
    match = _DURATION_RE.match(text or "")
    if not match or float(match.group(1)) <= 0:
        raise StaleError("durée illisible : %r (90m, 6h, 2d…)" % text)
    return float(match.group(1)) * _UNITS[match.group(2).lower()]


def stale_after(text: str | None = None, env: dict | None = None) -> float:
    """Le seuil : option, sinon `AMEESH_STALE_AFTER`, sinon 6 h."""
    env = os.environ if env is None else env
    value = text or env.get(STALE_ENV)
    return parse_duration(value) if value else DEFAULT_THRESHOLD_S


# --------------------------------------------------------------------------
# activité
# --------------------------------------------------------------------------

def last_activity(item: dict, events: Iterable[dict] = (), milestones: Iterable[dict] = (),
                  actions: Iterable[dict] = (), messages_ts: float | None = None
                  ) -> float | None:
    """Dernière activité : ligne du lot, transition ou note, jalon, action, message."""
    stamps = [item.get("updated_ts"), item.get("created_ts"), messages_ts]
    stamps += [e.get("created_ts") for e in events]
    stamps += [m.get("at_ts") for m in milestones]
    stamps += [a.get("updated_ts") or a.get("created_ts") for a in actions]
    stamps = [float(s) for s in stamps if s is not None]
    return max(stamps) if stamps else None


def stale(state: str, last_ts: float | None, now: float, threshold: float) -> dict | None:
    """`{since_ts, idle_s, threshold_s}` si le lot ouvert n'a rien fait depuis le seuil."""
    if state in DONE_STATES or last_ts is None:
        return None
    idle = now - float(last_ts)
    if idle < threshold:
        return None
    return {"since_ts": round(float(last_ts), 3), "idle_s": int(idle),
            "threshold_s": int(threshold)}


# --------------------------------------------------------------------------
# attente
# --------------------------------------------------------------------------

_LABELS = {
    "start": ("démarrage par %s", "attribution (lot non assigné)"),
    "build": ("travail de %s", "travail (lot non assigné)"),
    "verdict": ("verdict de %s", "verdict d'un relecteur (non désigné)"),
    "fix": ("correction de %s", "correction (auteur non désigné)"),
    "merge-approval": ("approbation de la fusion par %s", "approbation de la fusion"),
    "merge": ("fusion par %s", "fusion (exécutant non désigné)"),
    "merge-outcome": ("issue de la fusion inconnue : réconciliation par %s",
                      "issue de la fusion inconnue : réconciliation"),
    "decision": ("décision humaine de %s", "décision humaine"),
    "unblock": ("déblocage par %s", "déblocage"),
}


def _waiting(what: str, who) -> dict:
    if isinstance(who, (list, tuple)):
        who = ", ".join(str(w) for w in who if w) or None
    who = who or None
    with_who, without = _LABELS[what]
    return {"what": what, "who": who, "label": with_who % who if who else without}


def waiting(item: dict, jalons: dict, actions: Iterable[dict] = (), *,
            responsible: str | None = None) -> dict | None:
    """Ce qu'attend un lot ouvert, et de qui : `{what, who, label}` (voir le module).

    `jalons` : la forme de `progress._jalons_de_lot` ; `responsible` : le
    responsable humain de la fiche WorkPackage du lot, s'il en a une."""
    state = item.get("state") or ""
    assignee = item.get("assignee") or None
    if state in DONE_STATES:
        return None
    actions = list(actions)
    # 1. une action qui attend un humain passe avant tout (L26)
    for act in reversed(actions):
        if act.get("state") not in _HUMAN_ACTION_STATES:
            continue
        approvers = list(act.get("approvers") or []) or responsible
        if act.get("connector") == MERGE_CONNECTOR:
            if act["state"] == "proposed":
                return _waiting("merge-approval", approvers)
            return _waiting("merge-outcome", responsible)
        return _waiting("decision", approvers if act["state"] == "proposed" else responsible)
    if state == "waiting_human":
        return _waiting("decision", responsible)
    # 3. un verdict bloquant ne compte que s'il suit le dernier gel (L26)
    if jalons.get("verdict_kind") == "blocked" and jalons.get("verdict_current"):
        return _waiting("fix", assignee)
    if state == "blocked":
        return _waiting("unblock", responsible or assignee)
    if state == "intake":
        return _waiting("start", assignee)
    if state == "build":
        return _waiting("build", assignee)
    # qa : en revue
    if jalons.get("verdict_kind") == "ok" and jalons.get("verdict_current"):
        merges = [a for a in actions if a.get("connector") == MERGE_CONNECTOR]
        last = merges[-1] if merges else None
        if last is None or last.get("state") == "cancelled":
            return _waiting("merge", responsible)
        if last.get("state") == "approved":
            return _waiting("merge", last.get("proposed_by"))
        if last.get("state") == "failed":
            return _waiting("fix", assignee)
        return _waiting("merge", responsible)
    return _waiting("verdict", jalons.get("reviewer"))


def legacy(detail: dict | None) -> str | None:
    """Le résumé en un mot (forme L26, `ameesh-alert/1`)."""
    return _LEGACY.get((detail or {}).get("what"))


def waiting_for(item: dict, events: Iterable[dict], milestones: Iterable[dict],
                actions: Iterable[dict], *, responsible: str | None = None) -> str | None:
    """Forme d'origine de L26 : un mot de `WAITING_FOR`, ou None."""
    from . import progress  # import tardif : progress importe ce module

    actions = list(actions)
    jalons = progress._jalons_de_lot(item, list(events), actions, list(milestones))
    return legacy(waiting(item, jalons, actions, responsible=responsible))


# --------------------------------------------------------------------------
# lecture groupée (alerte stale_lot, work list, progress)
# --------------------------------------------------------------------------

def _grouped(st, ids: list[int]) -> tuple[dict, dict, dict, dict]:
    events: dict = {}
    milestones: dict = {}
    actions: dict = {}
    messages: dict = {}
    if ids:
        for ev in st.progress.lot_events(ids):
            events.setdefault(int(ev["work_item_id"]), []).append(ev)
        for ms in st.progress.lot_milestones(ids):
            milestones.setdefault(int(ms["work_item_id"]), []).append(ms)
        for act in st.progress.lot_actions(ids):
            actions.setdefault(int(act["work_item"]), []).append(act)
        for row in st.progress.lot_messages(ids):
            messages[int(row["work_item_id"])] = row.get("last_ts")
    return events, milestones, actions, messages


def describe(db, rows: list[dict], *, now: float | None = None,
             threshold: float = DEFAULT_THRESHOLD_S) -> dict[int, dict]:
    """Pour chaque lot (lignes de `work_items`) : `{waiting, waiting_for,
    last_activity_ts, stale}`. Une lecture groupée, aucune requête par lot."""
    from . import progress  # import tardif : progress importe ce module

    now = time.time() if now is None else float(now)
    st = storage.of(db)
    ids = [int(r["id"]) for r in rows]
    packages = {p["id"]: p for p in st.packages.all(include_absent=True)}
    events, milestones, actions, messages = _grouped(st, ids)
    out: dict[int, dict] = {}
    for row in rows:
        rid = int(row["id"])
        acts = actions.get(rid, [])
        jalons = progress._jalons_de_lot(row, events.get(rid, []), acts, milestones.get(rid))
        detail = waiting(row, jalons, acts,
                         responsible=(packages.get(row.get("package_id")) or {}).get(
                             "responsible"))
        last = last_activity(row, events.get(rid, []), milestones.get(rid, []), acts,
                             messages.get(rid))
        out[rid] = {"waiting": detail, "waiting_for": legacy(detail),
                    "last_activity_ts": round(last, 3) if last is not None else None,
                    "stale": stale(row.get("state") or "", last, now, threshold)}
    return out


def stagnant_lots(db, *, threshold_s: float = DEFAULT_THRESHOLD_S,
                  now: float | None = None, limit: int = 500) -> list[dict]:
    """Les lots ouverts inactifs depuis au moins `threshold_s` secondes.

    Une ligne : id, title, state, assignee, `last_activity_ts`, `idle_s`,
    `waiting_for` (un mot de `WAITING_FOR` ou None, forme L26) et `waiting`
    (`{what, who, label}`, L29)."""
    now = time.time() if now is None else float(now)
    st = storage.of(db)
    rows = [r for r in st.operations.open_lots_activity(limit)
            if r.get("last_activity_ts") is not None
            and now - float(r["last_activity_ts"]) >= float(threshold_s)]
    described = describe(db, rows, now=now, threshold=threshold_s)
    out = []
    for row in rows:
        lot = int(row["id"])
        info = described[lot]
        out.append({
            "id": lot,
            "title": row.get("title"),
            "state": row.get("state"),
            "assignee": row.get("assignee") or None,
            "last_activity_ts": float(row["last_activity_ts"]),
            "idle_s": int(now - float(row["last_activity_ts"])),
            "waiting_for": info["waiting_for"],
            "waiting": info["waiting"],
        })
    return out
