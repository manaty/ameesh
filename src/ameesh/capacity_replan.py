# SPDX-License-Identifier: AGPL-3.0-only
"""Event-driven capacity projection, reconciled by the existing issues pass.

The live input fingerprint is the event boundary. Clock ticks alone do not
move promises. Dates, lot journal entries and the checkpoint commit together.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from . import capacity_plan, storage, work, work_dependencies

EVENT = "capacity.plan"
# Preserve the existing scheduler ownership prefix: manual edits then use
# the same pinning/CAS rules as work schedule (#740).
SOURCE = "reference p80 (capacity plan)"


def latest(db, project):
    rows = storage.of(db).events.recent_type(EVENT, None, 1, object=project)
    return rows[0]["data"] if rows else None


def _fingerprint(snapshot):
    value = deepcopy(snapshot)
    value.pop("now", None)
    # The history report includes derived ages and presentation metadata.
    value["history"] = [{k: row.get(k) for k in
                          ("type", "change_size", "estimate_minutes", "actual_minutes", "late")}
                         for row in value.get("history", [])]
    value["lots"] = sorted(value.get("lots", []), key=lambda r: int(r["id"]))
    value["hosts"] = sorted(value.get("hosts", []), key=lambda r: r["name"])
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def live_snapshot(db, project, settings, now):
    settings = deepcopy(settings)
    settings["now"] = now
    # Settings may retain gate details for a lot that has just merged.
    rows = storage.of(db).work.project_plan_rows(project)
    ids = {int(row["id"]) for row in rows}
    active = []
    for row in settings.get("lots", []):
        if int(row["id"]) in ids:
            active.append(row)
            continue
        item = work.get(db, int(row["id"]))
        if item is None or item.get("state") not in work.lifecycle.COMPLETED:
            raise capacity_plan.PlanningError("settings reference lot outside project: %s" % row["id"])
    settings["lots"] = active
    for row in settings["lots"]:
        for gate in row.get("gates", []):
            if gate.get("decision_id") is not None:
                from . import decisions
                decision = decisions.get(db, int(gate["decision_id"]), now=now)
                if decision is None:
                    raise capacity_plan.PlanningError("unknown decision #%s" % gate["decision_id"])
                if str(decision.get("lot")) != str(row["id"]):
                    raise capacity_plan.PlanningError("decision does not belong to lot #%s" % row["id"])
                # Never trust a cached answer in the settings file.
                gate.pop("answered_ts", None)
                if decision.get("state") == "answered":
                    gate["answered_ts"] = decision["answer"]["ts"]
    return capacity_plan.project_snapshot(db, project, settings)


def reconcile(db, project, settings, *, now):
    if storage.is_remote(db):
        raise capacity_plan.PlanningError("automatic planning requires a transactional connection")
    with db.transaction() as tx:
        # Same lock as merge/dependency writes: concurrent projectors cannot
        # publish the same change, or observe half of a merge's graph update.
        work_dependencies.lock(tx)
        snapshot = live_snapshot(tx, project, settings, now)
        st = storage.of(tx)
        plans = {int(p["id"]): p for p in st.roadmap.item_plans(
            [row["id"] for row in snapshot["lots"]])}

        def writable(current):
            source = current.get("planned_source") or ""
            return source.startswith("reference p80") or (
                not current.get("planned_start") and not current.get("planned_end")
                and not source.startswith("set by "))

        ownership = {ident: writable(current) for ident, current in plans.items()}
        fingerprint = _fingerprint(dict(snapshot, date_ownership=ownership))
        previous = latest(tx, project) or {}
        if previous.get("fingerprint") == fingerprint:
            return {"changed": False, "published": []}
        forecast = capacity_plan.forecast(snapshot, previous=previous.get("forecast"))

        published = []
        old = {row["id"]: row for row in previous.get("forecast", {}).get("lots", [])}
        publication = {}
        for row in forecast["lots"]:
            ident = row["id"]
            current = plans.get(ident)
            if current is None:
                raise capacity_plan.PlanningError("lot #%s disappeared during planning" % ident)
            can_write = ownership[ident]
            dates = {"start": row["start"][:10], "end": row["end"][:10]}
            view = dict(dates, writable=can_write)
            publication[str(ident)] = view
            prior = (previous.get("publication") or {}).get(str(ident))
            if prior is None and ident in old:
                prior = {"start": old[ident]["start"][:10], "end": old[ident]["end"][:10],
                         "writable": can_write}
            if prior == view:
                continue
            if can_write and (current.get("planned_start"), current.get("planned_end")) != (
                    dates["start"], dates["end"]):
                if st.roadmap.plan_item(ident, dates, source=SOURCE,
                                        actor="ameesh", expect=current) is None:
                    raise capacity_plan.PlanningError("lot #%s dates changed concurrently" % ident)
            work.note(tx, ident, "Capacity forecast: %s → %s%s" % (
                row["start"], row["end"], " (manual dates kept)" if not can_write else ""),
                actor="ameesh")
            published.append(ident)
        # Checkpoint even a capacity change with identical dates, so its
        # replay remains silent. This internal event is not a lot publication.
        st.events.record(EVENT, project, project,
                         {"fingerprint": fingerprint, "forecast": forecast, "publication": publication})
        return {"changed": True, "published": published}


def tick(cfg, db, *, now, dry_run=False, log=lambda message: None):
    """Read configured files afresh; one bad project cannot stop another."""
    if dry_run:
        return
    planning = getattr(cfg, "project_planning", {}) or {}
    if not isinstance(planning, dict):
        log("capacity plan: project_planning must be an object")
        return
    for project, entry in planning.items():
        if not isinstance(entry, dict) or not entry.get("capacity_plan"):
            continue
        started = time.monotonic()
        try:
            settings = json.loads(Path(entry["capacity_plan"]).expanduser().read_text())
            result = reconcile(db, project, settings, now=now)
            log("capacity plan %s: %.3f s, %d lot date publication(s)" % (
                project, time.monotonic() - started, len(result["published"])))
        except Exception as exc:  # noqa: BLE001 - preserve previous published dates
            log("capacity plan %s: %s" % (project, exc))
