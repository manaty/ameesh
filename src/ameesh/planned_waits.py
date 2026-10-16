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
        return plan.day_start_ts((day + datetime.timedelta(days=1)).isoformat())
    except (TypeError, ValueError, OverflowError):
        return None


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
            landings=()):
    """Pure resolver. Only an open execution lot can block another lot.

    Commitments apply to their explicit work item or to their plan package
    (including descendants). Dependencies are an anchored numeric lot
    reference or an exact WorkPackage identifier. Unknown and self references
    cannot suppress an alert. Proposed/cancelled/done commitments are ignored.

    An open commitment whose `depends_on` names an external gate
    (`gate:<name>` or `external:<name>`) *holds* its lot until the end of the
    commitment's due day: the gate is not a lot, so nothing else can release
    it, and the date is what makes the hold end by itself.

    #1945: a declared landing (`landings`, rows of `work_landings` enriched
    with the vehicle's state and last freeze) is an admission PINNED to one
    freeze. It waits only while the vehicle is open, not `blocked`, its last
    freeze is exactly `pinned_sha`, and the finite day `until_on` is not past
    (its day counts in full, like a hold). A new freeze, a missing freeze or a
    blocked vehicle gives the signal back until the pilot renews the
    declaration explicitly. A scheduled start, a dependency or a gate stays
    ahead: the more explicit wait wins. Nothing is inferred from prose or git.
    """
    open_ids = {int(r["id"]) for r in open_items
                if r.get("state") not in ("merged", "promoted", "closed")}
    landing_by_item = {int(l["work_item_id"]): l for l in landings}
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
            continue
        # #1945 (revue B) : l'admission est épinglée au gel du véhicule. Elle
        # n'éteint que si le véhicule est ouvert, non bloqué, et si son dernier
        # gel est EXACTEMENT la tête épinglée ; l'échéance (journée finie,
        # comptée en entier) doit être future. Nouveau gel, gel absent, véhicule
        # bloqué, retrait ou échéance dépassée : le signal stale revient, et le
        # pilote renouvelle explicitement la déclaration sur la nouvelle tête.
        landing = landing_by_item.get(ident)
        if not landing:
            continue
        vehicle = int(landing["vehicle_id"])
        open_vehicle = landing.get("open")
        if open_vehicle is None:
            open_vehicle = vehicle in open_ids
        etat = landing.get("vehicle_state")
        pinned = (landing.get("pinned_sha") or "").strip()
        tete = (landing.get("head") or "").strip()
        end = hold_end(landing.get("until_on"))
        if (vehicle == ident or not open_vehicle or etat == "blocked"
                or not pinned or not tete or pinned != tete
                or end is None or now >= end):
            continue
        wait = {"what": "landing", "vehicle": vehicle,
                "until": str(landing.get("until_on"))[:10], "head": pinned}
        if etat:
            wait["step"] = etat
        waits[ident] = wait
    return waits


def _with_heads(st, landings):
    """#1945 : la tête exacte du véhicule est son dernier gel, lu en base.

    Une lecture groupée pour tous les véhicules déclarés, comparée à chaque
    passe à la tête épinglée dans `work_landings` (`pinned_sha`) : un nouveau
    gel ne prolonge donc rien sans renouvellement explicite. Les jalons non
    `frozen` ou sans sha ne comptent pas."""
    rows = list(landings)
    vehicles = sorted({int(l["vehicle_id"]) for l in rows})
    heads: dict[int, tuple[float, str]] = {}
    if vehicles:
        for mark in st.progress.lot_milestones(vehicles):
            sha = (mark.get("sha") or "").strip()
            if mark.get("kind") != "frozen" or not sha:
                continue
            vid = int(mark["work_item_id"])
            at = float(mark.get("at_ts") or 0)
            if vid not in heads or at >= heads[vid][0]:
                heads[vid] = (at, sha)
    out = []
    for landing in rows:
        head = heads.get(int(landing["vehicle_id"]))
        out.append(dict(landing, head=head[1]) if head else landing)
    return out


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
    landings = _with_heads(st, st.work_dependencies.landings_for([int(r["id"]) for r in rows]))
    return resolve(rows, dates, commitments, packages, open_items, now=now,
                   dependencies=dependencies, landings=landings)
