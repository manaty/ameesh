# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit planned waits for lots (L368); never infer dependencies from prose."""
from __future__ import annotations

import re
import time

from . import plan, storage

_LOT = re.compile(r"^(?:(?:task|lot)\s*#?\s*|#)?([0-9]{1,18})$", re.I)
_PACKAGE = re.compile(r"^(?:lot|package|workpackage)\s+(\S+)$", re.I)


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


def resolve(rows, dates, commitments, packages, open_items, *, now):
    """Pure resolver. Only an open execution lot can block another lot.

    Commitments apply to their explicit work item or to their plan package
    (including descendants). Dependencies are an anchored numeric lot
    reference or an exact WorkPackage identifier. Unknown and self references
    cannot suppress an alert. Proposed/cancelled/done commitments are ignored.
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
    return waits


def read(db, rows, *, now=None, packages=None, open_items=None):
    """Grouped, unbounded truth reads; no query per candidate or dependency."""
    if not rows:
        return {}
    now = time.time() if now is None else float(now)
    st = storage.of(db)
    dates = {int(r["id"]): r for r in st.roadmap.item_plans([int(r["id"]) for r in rows])}
    commitments = st.roadmap.commitments(statuses=["open"])
    packages = (packages if packages is not None else
                {p["id"]: p for p in st.packages.all(include_absent=True)})
    if open_items is None:
        open_items = st.operations.open_lots_activity(None) if any(
            c.get("depends_on") for c in commitments) else []
    return resolve(rows, dates, commitments, packages, open_items, now=now)
