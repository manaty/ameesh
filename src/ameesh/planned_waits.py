# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit planned waits for lots (L368); never infer dependencies from prose."""
from __future__ import annotations

import datetime
import re
import time

from . import plan, storage

_LOT = re.compile(r"^(?:(?:task|lot)\s*#?\s*|#)?([0-9]{1,18})$", re.I)
_PACKAGE = re.compile(r"^(?:lot|package|workpackage)\s+(\S+)$", re.I)
#: external hold: the commitment names the gate it waits for (`gate:<name>`,
#: or `external:<name>`), and holds the lot until the end of its due day.
_GATE = re.compile(r"^(?:gate|external):(\S+)$", re.I)


def hold_end(due_on):
    """End of the due day (the day counts in full), or None without a date.

    A hold always ends: past this instant the ordinary inactivity rule
    applies again, like `engagement_overdue`, which also counts the due day
    in full and only fires the next day.
    """
    try:
        day = datetime.date.fromisoformat(str(due_on)[:10])
    except (TypeError, ValueError):
        return None
    return plan.day_start_ts((day + datetime.timedelta(days=1)).isoformat())


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


def resolve(rows, dates, commitments, packages, open_items, *, now, dependencies=()):
    """Pure resolver. Only an open execution lot can block another lot.

    Commitments apply to their explicit work item or to their plan package
    (including descendants). Dependencies are an anchored numeric lot
    reference or an exact WorkPackage identifier. Unknown and self references
    cannot suppress an alert. Proposed/cancelled/done commitments are ignored.

    An open commitment whose `depends_on` names an external gate
    (`gate:<name>` or `external:<name>`) *holds* its lot until the end of the
    commitment's due day: the gate is not a lot, so nothing else can release
    it, and the date is what makes the hold end by itself.
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
    gate_item, gate_package = {}, {}
    for edge in dependencies:
        if edge.get("blocking"):
            direct.setdefault(int(edge["work_item_id"]), set()).add(int(edge["blocker_id"]))
    for commitment in commitments:
        if commitment.get("status") != "open":
            continue
        blockers = set()
        gate = None
        for reference in commitment.get("depends_on") or []:
            if not isinstance(reference, str):
                continue
            reference = reference.strip()
            match = _GATE.fullmatch(reference)
            if match:
                gate = gate or match[1]
                continue
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
        end = hold_end(commitment.get("due_on")) if gate else None
        if end is not None and now < end:
            held = {"gate": gate, "until": str(commitment.get("due_on"))[:10]}
            if commitment.get("id") is not None:
                held["commitment"] = commitment["id"]
            if commitment.get("work_item_id") is not None:
                gate_item.setdefault(int(commitment["work_item_id"]), held)
            elif commitment.get("package_id"):
                gate_package.setdefault(commitment["package_id"], held)
    waits = {}
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
        blockers = set(direct.get(ident, ()))
        for package_id in ancestors(row.get("package_id"), packages):
            blockers.update(by_package.get(package_id, ()))
        blockers.discard(ident)
        if blockers:
            waits[ident] = {"what": "dependency", "lots": sorted(blockers)}
            continue
        held = gate_item.get(ident)
        if held is None:
            for package_id in ancestors(row.get("package_id"), packages):
                held = gate_package.get(package_id)
                if held:
                    break
        if held:
            waits[ident] = dict(held, what="hold")
    return waits


def read(db, rows, *, now=None, packages=None, open_items=None, dependencies=None):
    """Grouped, unbounded truth reads; no query per candidate or dependency."""
    if not rows:
        return {}
    now = time.time() if now is None else float(now)
    st = storage.of(db)
    if dependencies is None:
        dependencies = st.work_dependencies.for_items([int(r["id"]) for r in rows])
    dates = {int(r["id"]): r for r in st.roadmap.item_plans([int(r["id"]) for r in rows])}
    commitments = st.roadmap.commitments(statuses=["open"])
    packages = (packages if packages is not None else
                {p["id"]: p for p in st.packages.all(include_absent=True)})
    if open_items is None:
        open_items = st.operations.open_lots_activity(None) if any(
            c.get("depends_on") for c in commitments) else []
    return resolve(rows, dates, commitments, packages, open_items, now=now,
                   dependencies=dependencies)
