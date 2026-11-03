# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic capacity forecast; no dispatch, network or database mutations.

Times are UTC epoch seconds; estimates and gate delays are elapsed minutes.
A fresh snapshot is the event boundary: completed_ts, answered_ts, estimates
and host capacity are read again, never incrementally patched into old dates.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
from statistics import median

from . import slot_reservations
from .work_footprints import held_pairs

SCHEMA = "ameesh-capacity-plan/1"


class PlanningError(ValueError):
    """Invalid or incomplete planning inputs; never fabricate an ETA."""


def number(value, label, *, positive=False):
    if isinstance(value, bool):
        raise PlanningError("%s must be a finite number" % label)
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise PlanningError("%s must be a finite number" % label) from None
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise PlanningError("%s must be %s" % (label, "positive" if positive else "nonnegative"))
    return result


def integer(value, label):
    result = number(value, label)
    if result != int(result):
        raise PlanningError("%s must be an integer" % label)
    return int(result)


def calibrated_minutes(lot, history):
    """Median actual/estimated ratio, same type/size, then type, then overall.

    Late estimates do not calibrate earlier predictions. Return the cohort
    size and ratio with every duration so the forecast remains inspectable.
    """
    samples = [h for h in history if not h.get("late")
               and h.get("estimate_minutes") and h.get("actual_minutes") is not None]
    for h in samples:
        number(h["estimate_minutes"], "historical estimate", positive=True)
        number(h["actual_minutes"], "actual duration")
    cohort = next((group for group in (
        [h for h in samples if (h.get("type"), h.get("change_size")) ==
         (lot.get("type"), lot.get("change_size"))],
        [h for h in samples if h.get("type") == lot.get("type")], samples) if group), [])
    ratio = median([float(h["actual_minutes"]) / float(h["estimate_minutes"])
                    for h in cohort]) if cohort else 1.0
    estimate = number(lot.get("estimate_minutes"), "lot #%s estimate" % lot["id"], positive=True)
    # A measured zero is valid history, but a run always occupies some time.
    return {"estimate_minutes": estimate, "calibration_ratio": ratio,
            "calibration_samples": len(cohort), "duration_minutes": max(estimate * ratio, 1 / 60)}


def _slots(hosts):
    slots = []
    names = set()
    for host in hosts:
        name = str(host["name"])
        if name in names:
            raise PlanningError("duplicate host %s" % name)
        names.add(name)
        count = integer(host["slots"], "host slots")
        reserved = host.get("reservations", {})
        if sum(integer(n, "reservation") for n in reserved.values()) > count:
            raise PlanningError("reservations exceed capacity on %s" % name)
        # #5601: `project:<app>` and `class:<p>` are the live dispatcher's
        # keys (`slot_reservations`); a bare name stays a project/repository
        owners = [slot_reservations.normalize_key(owner) or str(owner)
                  for owner, n in sorted(reserved.items())
                  for _ in range(integer(n, "reservation"))]
        owners += [None] * (count - len(owners))
        slots.extend({"host": name, "slot": i + 1, "reservation": owner}
                     for i, owner in enumerate(owners))
    if not slots:
        raise PlanningError("at least one run slot is required")
    return slots


def _stamp(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _compute(snapshot):
    if not isinstance(snapshot, dict):
        raise PlanningError("snapshot must be a JSON object")
    now = number(snapshot.get("now"), "now")
    slots = _slots(snapshot["hosts"])
    rows = deepcopy(snapshot.get("lots", []))
    lots = {}
    for row in rows:
        ident = integer(row["id"], "lot id")
        if ident in lots:
            raise PlanningError("duplicate lot #%s" % ident)
        row["id"] = ident
        lots[ident] = row
    deps = {i: set(integer(d, "dependency") for d in r.get("depends_on", []))
            for i, r in lots.items()}
    conflicts = {i: set(integer(d, "conflict") for d in r.get("conflicts_with", []))
                 for i, r in lots.items()}
    for link in snapshot.get("links", []):
        left, right = integer(link["source"], "link source"), integer(link["target"], "link target")
        if left not in lots or right not in lots:
            raise PlanningError("typed link references an unknown lot")
        kind = link["kind"]
        if kind == "depends_on":
            deps[left].add(right)
        elif kind == "conflicts_with":
            conflicts[left].add(right)
        elif kind == "part_of":
            lots[left]["epic"] = right
        elif kind not in ("duplicates", "supersedes"):
            raise PlanningError("unknown link kind %s" % kind)
    for ident in lots:
        unknown = (deps[ident] | conflicts[ident]) - lots.keys()
        if unknown or ident in deps[ident] or ident in conflicts[ident]:
            raise PlanningError("invalid dependencies/conflicts for #%s: %s" % (ident, sorted(unknown)))
        for other in list(conflicts[ident]):
            conflicts[other].add(ident)
    # Reject cycles even when their nodes have already completed.
    topo, pending = [], set(lots)
    while pending:
        roots = sorted(i for i in pending if not deps[i] & pending)
        if not roots:
            raise PlanningError("dependency cycle: %s" % sorted(pending))
        topo.extend(roots)
        pending.difference_update(roots)
    history = snapshot.get("history", [])
    durations = {i: calibrated_minutes(r, history) for i, r in lots.items()
                 if r.get("completed_ts") is None}
    policies = snapshot.get("policies", {})
    classes = snapshot.get("project_classes", {})
    scheduled = {}
    intervals = [[] for _ in slots]

    def clashing(a, b):
        if b["id"] in conflicts[a["id"]]:
            return True
        repo = a.get("repository")
        if not repo or repo != b.get("repository"):
            return False
        policy = policies.get(repo, {})
        unknown = ["."] if policy.get("unknown_footprint") == "repository" else []
        return bool(held_pairs(a.get("footprint_paths") or unknown,
                               b.get("footprint_paths") or unknown,
                               policy.get("shared_paths", [])))

    def priority_class(row):
        return integer(row.get("priority_class", classes.get(str(row.get("project", "")), 2)),
                       "priority class")

    def admission(i):
        row = lots[i]
        return (row.get("started_ts") is None, priority_class(row),
                number(row.get("created_ts", 0), "created_ts"), i)

    pending = set(lots)
    while pending:
        ready = [i for i in pending if deps[i] <= scheduled.keys()]
        ident = min(ready, key=admission)
        row = lots[ident]
        pending.remove(ident)
        if row.get("completed_ts") is not None:
            end = number(row["completed_ts"], "completed_ts")
            if end > now:
                raise PlanningError("completed_ts cannot be in the future")
            scheduled[ident] = dict(id=ident, start_ts=end, end_ts=end, start=_stamp(end),
                                    end=_stamp(end), completed=True,
                                    host=None, slot=None, causes=[], epic=row.get("epic"))
            continue
        release = max(now, number(row.get("not_before_ts", now), "not_before_ts"))
        causes = []
        for dep in sorted(deps[ident]):
            end = scheduled[dep]["end_ts"]
            if end > release:
                release, causes = end, [dep]
            elif end == release:
                causes.append(dep)
        gate_end = release
        for gate in row.get("gates", []):
            if gate.get("answered_ts") is not None:
                end = number(gate["answered_ts"], "answered_ts")
                if end > now:
                    raise PlanningError("answered_ts cannot be in the future")
            else:
                approver = gate.get("approver")
                delay = gate.get("response_minutes", snapshot.get("approver_minutes", {}).get(approver))
                end = max(release, number(gate.get("requested_ts", release), "requested_ts")
                          + number(delay, "response_minutes for %s" % approver) * 60)
            gate_end = max(gate_end, end)
        duration = durations[ident]["duration_minutes"] * 60
        if row.get("started_ts") is not None:
            started = number(row["started_ts"], "started_ts")
            if started > now:
                raise PlanningError("started_ts cannot be in the future")
            duration = max(duration - (now - started), 1.0)
        project, repository = str(row.get("project", "")), str(row.get("repository", ""))
        owners = {None, project, repository, "project:" + repository,
                  *slot_reservations.keys_for(project, priority_class(row))}
        options = []
        for index, slot in enumerate(slots):
            if slot["reservation"] not in owners:
                continue
            if row.get("host") and row["host"] != slot["host"]:
                continue
            if row.get("slot") is not None and integer(row["slot"], "pinned slot") != slot["slot"]:
                continue
            # Earliest gap on this slot, plus all cross-slot footprint blockers.
            blockers = list(intervals[index])
            blockers += [entry for entry in scheduled.values()
                         if not entry.get("completed") and clashing(row, lots[entry["id"]])]
            start, why = gate_end, list(causes)
            for block in sorted(blockers, key=lambda b: (b["start_ts"], b["end_ts"], b["id"])):
                if start < block["end_ts"] and start + duration > block["start_ts"]:
                    start, why = block["end_ts"], [block["id"]]
            options.append((start, index, why))
        if not options:
            raise PlanningError("no eligible slot for lot #%s (host/reservations)" % ident)
        start, index, why = min(options, key=lambda o: (o[0], o[1]))
        if row.get("started_ts") is not None and start > now:
            raise PlanningError("in-progress lot #%s cannot fit current capacity/gates" % ident)
        entry = dict(id=ident, start_ts=start, end_ts=start + duration,
                     start=_stamp(start), end=_stamp(start + duration),
                     host=slots[index]["host"], slot=slots[index]["slot"],
                     reservation=slots[index]["reservation"], causes=why,
                     gate_wait_minutes=(gate_end - release) / 60,
                     epic=row.get("epic"), completed=False, **durations[ident])
        scheduled[ident] = entry
        intervals[index].append(entry)
    entries = sorted(scheduled.values(), key=lambda e: (e["start_ts"], e["id"]))
    finish = max([now] + [e["end_ts"] for e in entries])
    path = []
    if entries:
        cursor = max(entries, key=lambda e: (e["end_ts"], -e["id"]))
        while not cursor.get("completed"):
            path.append(cursor["id"])
            if not cursor["causes"]:
                break
            cursor = scheduled[min(cursor["causes"])]
    epics = {}
    for e in entries:
        if e["epic"] is not None:
            key = str(e["epic"])
            group = epics.setdefault(key, {"start_ts": e["start_ts"], "end_ts": e["end_ts"], "lots": []})
            group["start_ts"] = min(group["start_ts"], e["start_ts"])
            group["end_ts"] = max(group["end_ts"], e["end_ts"])
            group["lots"].append(e["id"])
    for group in epics.values():
        group.update(start=_stamp(group["start_ts"]), end=_stamp(group["end_ts"]))
    waves = {}
    for e in entries:
        if not e.get("completed"):
            waves.setdefault(e["start_ts"], []).append(e["id"])
    return dict(schema=SCHEMA, now=now, end_ts=finish, end=_stamp(finish), lots=entries,
                epics=epics, critical_path=list(reversed(path)),
                waves=[{"start_ts": ts, "lots": ids} for ts, ids in sorted(waves.items())],
                slots=len(slots), method="deterministic earliest-gap list scheduling; feasible, not optimal")


def forecast(snapshot, *, previous=None):
    """Recompute and publish a JSON-compatible view; optionally size extra slots.

    Extra capacity is unreserved and available to any host-unpinned run.
    Report the smallest *tested* feasible capacity; do not claim optimality.
    Dependencies, human waits and footprints can make a deadline impossible.
    """
    out = _compute(snapshot)
    old = {r["id"]: r for r in (previous or {}).get("lots", [])}
    out["moved"] = [{"id": r["id"], "previous_start_ts": old[r["id"]]["start_ts"],
                     "start_ts": r["start_ts"], "previous_end_ts": old[r["id"]]["end_ts"],
                     "end_ts": r["end_ts"]} for r in out["lots"] if r["id"] in old and
                    (r["start_ts"], r["end_ts"]) != (old[r["id"]]["start_ts"], old[r["id"]]["end_ts"])]
    if snapshot.get("deadline_ts") is not None:
        deadline = number(snapshot["deadline_ts"], "deadline_ts")
        result = {"deadline_ts": deadline, "met": out["end_ts"] <= deadline,
                  "required_slots": out["slots"] if out["end_ts"] <= deadline else None,
                  "additional_slots": 0 if out["end_ts"] <= deadline else None}
        if not result["met"]:
            # More slots than unfinished tasks cannot add useful parallelism.
            for extra in range(1, len(snapshot.get("lots", [])) + 1):
                candidate = deepcopy(snapshot)
                names = {h["name"] for h in candidate["hosts"]}
                name = "additional-capacity"
                while name in names:
                    name += "-extra"
                candidate["hosts"].append({"name": name, "slots": extra})
                trial = _compute(candidate)
                if trial["end_ts"] <= deadline:
                    result.update(required_slots=trial["slots"], additional_slots=extra,
                                  achievable_end_ts=trial["end_ts"])
                    break
            if result["required_slots"] is None:
                result["reason"] = "deadline not reached by adding slots; inspect dependencies, gates, footprints and host pins"
        out["deadline"] = result
    return out


def run_file(path, *, previous_path=None, db=None, project=None, canons=None):
    """CLI boundary: read snapshots and print only the computed projection."""
    import json
    from pathlib import Path
    snapshot = json.loads(Path(path).read_text())
    if project:
        snapshot = project_snapshot(db, project, snapshot, canons=canons)
    previous = json.loads(Path(previous_path).read_text()) if previous_path else None
    return forecast(snapshot, previous=previous)


def project_snapshot(db, project, settings, *, canons=None):
    """Read the existing backlog and recorded actuals for a capacity forecast.

    Settings supply hosts, now, gate response times and per-lot gate details.
    Missing cross-project blockers or unmodelled human waits are rejected;
    incomplete inputs must not turn into promises of delivery.
    """
    from . import estimates, storage, work_dependencies
    out = deepcopy(settings)
    # #5601: a host without explicit reservations gets those of its Host
    # records, the ones the live dispatcher enforces
    for host in out.get("hosts", []):
        if "reservations" not in host and canons:
            reserved = slot_reservations.merge(
                one.host(str(host["name"])).policy.slot_reservations or {}
                for one in canons if one.host(str(host["name"])) is not None)
            if reserved:
                host["reservations"] = reserved
    overrides = {integer(r["id"], "lot override"): r for r in out.pop("lots", [])}
    rows = storage.of(db).work.project_plan_rows(project)
    ids = {int(row["id"]) for row in rows}
    unknown = overrides.keys() - ids
    if unknown:
        raise PlanningError("settings reference lots outside project: %s" % sorted(unknown))
    links = work_dependencies.links(db, ids)
    out["lots"] = []
    for row in rows:
        ident = int(row["id"])
        item = {key: row.get(key) for key in ("id", "type", "change_size", "estimate_minutes",
                                             "created_ts", "footprint_paths")}
        item.update(project=project, repository=row.get("app") or project,
                    depends_on=list(row.get("blocking_dependencies") or []),
                    conflicts_with=list(row.get("open_conflicts") or []),
                    epic=row.get("package_id") or row.get("package_parent"))
        for link in links.get(ident, []):
            if link["kind"] == "part_of" and link["forward"]:
                item["epic"] = link["lot"]
        started = row.get("started_ts")
        if started is None and row.get("started_at") is not None:
            started = row["started_at"].timestamp()
        if started is not None:
            item["started_ts"] = started
        overlay = overrides.get(ident, {})
        # The database owns graph, estimate and footprint; settings own only
        # gates and resource constraints, never an imported stale copy of work.
        for key in ("gates", "host", "slot", "priority_class", "not_before_ts"):
            if key in overlay:
                item[key] = deepcopy(overlay[key])
        if (row.get("pending_decision") or row.get("decision_milestone")
                or row.get("state") in ("waiting_human", "blocked")) and not item.get("gates"):
            raise PlanningError("lot #%s needs explicit gate/wait details" % ident)
        out["lots"].append(item)
    out["history"] = estimates.report(db, app=project, now=out.get("now"))["lots"]
    return out
