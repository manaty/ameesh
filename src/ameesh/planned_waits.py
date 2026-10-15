# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit planned waits for lots (L368); never infer dependencies from prose.

A wait is declared, never guessed: a future planned start, an open commitment,
an explicit blocking dependency — or an explicit **hold** (#1664), the note a
pilot writes when the lot is parked on a gate no open lot carries (operator
window, human decision). The hold stops the stale reminders and the SLO
breaches, like any other planned wait, until the lot changes state or the hold
is explicitly lifted. Only a pilot's hold counts here (`pilot_holds`), and the
write path authenticates that claim (`work.check_wait_note`); the runner wake
keeps its own, older convention (`LotSessions.HOLD_PREFIX`)."""
from __future__ import annotations

import re
import time

from . import plan, storage
from .storage.interface import LotSessions

_LOT = re.compile(r"^(?:(?:task|lot)\s*#?\s*|#)?([0-9]{1,18})$", re.I)
_PACKAGE = re.compile(r"^(?:lot|package|workpackage)\s+(\S+)$", re.I)
#: #1664 : le préfixe de la note de retenue — le même que celui du réveil
#: (règle 1, `LotSessions.HOLD_PREFIX`).
_HOLD = re.compile("^" + re.escape(LotSessions.HOLD_PREFIX), re.I)
#: les notes de levée explicite ; un changement d'état du lot lève aussi.
_LIFT = re.compile(r"^(?:unhold|hold\s+(?:lifted|released|cleared))\s*:", re.I)
#: #1664 : motifs donnés au stockage pour la lecture groupée ciblée
#: (`Progress.lot_hold_events`) — exactement `_HOLD` et `_LIFT` (syntaxe `re`
#: côté repli, ARE côté pilote PostgreSQL) : toutes les notes hold/levée,
#: aucune note ordinaire. Le filtre exact reste ici.
HOLD_NOTE_PATTERNS = (_HOLD.pattern, _LIFT.pattern)


def is_hold_note(text) -> bool:
    """#1664 : la note qui pose une retenue (`hold: <raison>`). Le préfixe est
    celui du réveil (règle 1) ; seule la retenue d'un pilote compte pour la
    stagnation (`pilot_holds`)."""
    return bool(_HOLD.match(str(text or "").strip()))


def is_lift_note(text) -> bool:
    """#1664 : la note qui lève explicitement une retenue (`unhold:`,
    `hold lifted:`/`released:`/`cleared:`)."""
    return bool(_LIFT.match(str(text or "").strip()))


def explicit_holds(events, *, allowed=None) -> dict[int, dict]:
    """Les holds lus dans le journal d'un lot (#1664).

    Un hold tient tant que le lot ne change pas d'état et qu'aucune note de
    levée (`unhold:`, `hold lifted:`…) ne suit ; les notes ordinaires (triage,
    relevé) ne l'effacent pas. `allowed` restreint qui peut poser ou lever le
    hold : un prédicat (nom → bool) ou un ensemble de noms ; None = lecture
    brute, sans contrôle d'auteur. Les transitions d'état sont lues dans
    l'ordre d'écriture `(created_ts, id)` du journal. Le dernier hold posé
    porte l'auteur et la date affichés."""
    ordered: dict[int, list[dict]] = {}
    for event in events or ():
        ordered.setdefault(int(event["work_item_id"]), []).append(event)
    out: dict[int, dict] = {}
    for ident, lot_events in ordered.items():
        held = None
        previous = None
        for event in sorted(lot_events, key=lambda e: (float(e.get("created_ts") or 0.0),
                                                       int(e.get("id") or 0))):
            state = event.get("state")
            if previous is not None and state != previous:
                held = None                 # le lot a repris : ce qui le tenait est fini
            previous = state
            text = str(event.get("note") or "").strip()
            if not text:
                continue
            who = event.get("actor") or ""
            if allowed is not None and not (allowed(who) if callable(allowed)
                                            else who in allowed):
                continue
            if is_hold_note(text):
                held = {"what": "hold", "by": who or None,
                        "since": event.get("created_ts")}
            elif is_lift_note(text):
                held = None
        if held:
            out[ident] = held
    return out


def pilot_holds(db, events, *, cfg=None) -> dict[int, dict]:
    """Les holds posés par un pilote (#1664) — les seuls comptés par la
    stagnation.

    `events` est le flux ciblé lu par le stockage
    (`Progress.lot_hold_events` : transitions d'état + notes hold/levée), ou
    un journal complet ; les deux donnent le même hold.

    Même autorisation que la correction d'état et la désignation du relecteur
    (`work.corrector`, rôles `work.CORRECTOR_ROLES`) : un humain, ou un agent
    orchestrateur/conception déclaré (`AMEESH_ALERT_ORCHESTRATORS`, fiche Agent
    du canon), sans l'historique des lots confiés. Un hold d'un autre acteur
    garde son effet sur le réveil (règle 1) mais ne supprime pas l'alerte."""
    events = list(events or ())
    if not any(is_hold_note(event.get("note")) for event in events):
        return {}
    cfg = getattr(db, "cfg", None) if cfg is None else cfg
    from . import assignments, work            # imports tardifs : pas de cycle
    verdict: dict[str, bool] = {}

    def allowed(who: str) -> bool:
        if who not in verdict:
            try:
                verdict[who] = bool(who) and (
                    who.startswith(work.HUMAN_PREFIX)
                    or assignments.is_orchestrator(cfg, db, who,
                                                   roles=work.CORRECTOR_ROLES,
                                                   history=False))
            except Exception:
                verdict[who] = False           # autorisation illisible : pas de hold
        return verdict[who]

    return explicit_holds(events, allowed=allowed)


def ancestors(package_id, packages):
    """The package and its parents, cycle-safe; absent cards are not a plan."""
    seen = set()
    while package_id and package_id not in seen:
        package = packages.get(package_id)
        if not package or not package.get("present", True):
            break
        seen.add(package_id)
        yield package_id
        package_id = package.get("parent")


def resolve(rows, dates, commitments, packages, open_items, *, now, dependencies=(),
            holds=None):
    """Pure resolver. Only an open execution lot can block another lot.

    Commitments apply to their explicit work item or to their plan package
    (including descendants). Dependencies are an anchored numeric lot
    reference or an exact WorkPackage identifier. Unknown and self references
    cannot suppress an alert. Proposed/cancelled/done commitments are ignored.
    `holds` (#1664, `pilot_holds`) est un hold déclaré, prioritaire sur une
    dépendance : le lot est parqué, pas en attente d'un lot.
    """
    open_ids = {int(r["id"]) for r in open_items
                if r.get("state") not in ("merged", "promoted", "closed")}
    package_lots = {}
    for row in open_items:
        if int(row["id"]) not in open_ids:
            continue
        for package_id in ancestors(row.get("package_id"), packages):
            package_lots.setdefault(package_id, set()).add(int(row["id"]))
    direct, by_package = {}, {}
    for edge in dependencies:
        if edge.get("blocking"):
            direct.setdefault(int(edge["work_item_id"]), set()).add(int(edge["blocker_id"]))
    for commitment in commitments:
        if commitment.get("status") != "open":
            continue
        blockers = set()
        for reference in commitment.get("depends_on") or []:
            if not isinstance(reference, str):
                continue
            reference = reference.strip()
            match = _LOT.fullmatch(reference)
            if match:
                blockers.update({int(match[1])} & open_ids)
            else:
                package_ref = _PACKAGE.fullmatch(reference)
                blockers.update(package_lots.get(package_ref[1] if package_ref else reference, ()))
        if commitment.get("work_item_id") is not None:
            direct.setdefault(int(commitment["work_item_id"]), set()).update(blockers)
        elif commitment.get("package_id"):
            by_package.setdefault(commitment["package_id"], set()).update(blockers)
    waits = {}
    held = holds or {}
    for row in rows:
        ident = int(row["id"])
        if row.get("state") in ("merged", "promoted", "closed"):
            continue
        start = dates.get(ident, {}).get("planned_start")
        start_package = None
        if not start:
            for package_id in ancestors(row.get("package_id"), packages):
                start = plan.planned_dates(packages[package_id])["start"]
                if start:
                    start_package = package_id
                    break
        start_ts = plan.day_start_ts(start)
        if start_ts is not None and start_ts > now:
            waits[ident] = {"what": "scheduled", "start": start}
            if start_package:
                waits[ident]["package"] = start_package
            continue
        if ident in held:
            waits[ident] = dict(held[ident])
            continue
        blockers = set(direct.get(ident, ()))
        for package_id in ancestors(row.get("package_id"), packages):
            blockers.update(by_package.get(package_id, ()))
        blockers.discard(ident)
        if blockers:
            waits[ident] = {"what": "dependency", "lots": sorted(blockers)}
    return waits


def read(db, rows, *, now=None, packages=None, open_items=None, dependencies=None,
         holds=None):
    """Grouped, unbounded truth reads; no query per candidate or dependency.

    `holds` peut être fourni par un appelant qui a déjà lu le flux ciblé
    (`pilot_holds`) : sinon il est lu ici, en une lecture groupée (#1664) qui
    ne transfère que les transitions et les notes hold/levée — jamais le
    journal entier ni le texte des notes ordinaires."""
    if not rows:
        return {}
    now = time.time() if now is None else float(now)
    st = storage.of(db)
    ids = [int(r["id"]) for r in rows]
    if dependencies is None:
        dependencies = st.work_dependencies.for_items(ids)
    if holds is None:
        holds = pilot_holds(db, st.progress.lot_hold_events(ids, HOLD_NOTE_PATTERNS))
    dates = {int(r["id"]): r for r in st.roadmap.item_plans(ids)}
    commitments = st.roadmap.commitments(statuses=["open"])
    packages = (packages if packages is not None else
                {p["id"]: p for p in st.packages.all(include_absent=True)})
    if open_items is None:
        open_items = st.operations.open_lots_activity(None) if any(
            c.get("depends_on") for c in commitments) else []
    return resolve(rows, dates, commitments, packages, open_items, now=now,
                   dependencies=dependencies, holds=holds)
