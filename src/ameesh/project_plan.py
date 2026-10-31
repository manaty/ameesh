# SPDX-License-Identifier: AGPL-3.0-only
"""#4174: project readiness and bug-first dispatch, recomputed on every read."""
from __future__ import annotations

import datetime as dt
import time

from . import storage

SCHEMA = "ameesh-project-plan/1"


def spec_reason(item, rule):
    """Project-owned acceptance rule; no network or executable configuration.

    Existing projects accept the spec at intake. A project may instead require
    a literal marker in its source/body, including an adapter's imported proof.
    Unknown rules fail closed and explain the required configuration.
    """
    if rule is None or rule == {"mode": "intake"}:
        return ""
    if not isinstance(rule, dict) or rule.get("mode") != "marker" \
            or rule.get("field") not in ("body", "source") \
            or not isinstance(rule.get("value"), str) or not rule["value"].strip():
        return "invalid project spec_acceptance rule"
    if rule["value"] not in str(item.get(rule["field"]) or ""):
        return "spec acceptance missing: %s must contain %r" % (rule["field"], rule["value"])
    return ""


def order_key(item):
    """L165 and the plan view: priority, planned dates, age, lot number — one key."""
    return (int(item.get("priority") or 2),
            str(item.get("planned_start") or "9999-99-99"),
            str(item.get("planned_end") or "9999-99-99"),
            str(item.get("planned_delivery") or "9999-99-99"),
            float(item.get("created_ts") or 0), int(item["id"]))


def _disjoint_selection(ordered, policies):
    """#4818: keep a set of ready lots whose footprints are pairwise disjoint.

    The SQL guard already holds an intake lot that overlaps an *occupying*
    lot; two free lots that overlap each other are both ready, and the
    dispatcher would start both in the same pass. Walking the dispatch order,
    a free intake lot that overlaps one already selected waits for it, and
    the repository's in-flight cap counts the selected lots on top of those
    already in flight. Same overlap rule as the guard (`held_pairs`, `.`
    overlaps everything, shared paths ignored)."""
    from . import work_footprints
    selected: dict = {}
    for row in ordered:
        if not row["ready"] or row.get("state") != "intake" or row.get("assignee"):
            continue
        app = str(row.get("app") or "").strip()
        if not app:
            continue
        policy = (policies or {}).get(app) or {}
        mine = list(row.get("footprint_paths") or ())
        others = selected.setdefault(app, [])
        clash = [other for other in others
                 if mine and other.get("footprint_paths")
                 and work_footprints.held_pairs(mine, other["footprint_paths"],
                                                policy.get("shared_paths") or ())]
        if clash:
            row["ready"] = False
            row["waiting"].append("footprint overlaps %s, dispatched first"
                                  % ", ".join("#%s" % o["id"] for o in clash))
            continue
        cap = policy.get("max_in_flight")
        counted = [o for o in others if not work_footprints.is_exempt(o)]
        if cap and not work_footprints.is_exempt(row) and \
                int(row.get("repository_in_flight") or 0) + len(counted) >= int(cap):
            row["ready"] = False
            row["waiting"].append("repository in-flight cap %s reached with %s"
                                  % (cap, ", ".join("#%s" % o["id"] for o in counted)))
            continue
        others.append(row)


def evaluate(rows, *, project, rule=None, holds=None, now=None, can_take=None, phase=True,
             policies=None):
    """Readiness, optionally scoped to an agent before bug preemption.

    ``phase=False`` retains both kinds for the initial dispatcher candidate
    pool; each agent applies its own phase without deleting others' candidates.
    #4818: ``policies`` (repository footprint policies, by app) let the plan
    pick a set of ready lots with pairwise disjoint footprints.
    """
    from . import backlog
    stamp = time.time() if now is None else float(now)
    today = dt.datetime.fromtimestamp(stamp).date().isoformat()
    entries = []
    for row in rows:
        reasons = []
        state = row["state"]
        if state not in ("intake", "build"):
            reasons.append("state: %s" % state)
        if row.get("assignee"):
            reasons.append("assigned to %s" % row["assignee"])
        if state == "build" and not row.get("returned_to_queue"):
            reasons.append("already in progress")
        if row.get("external_executor"):
            reasons.append("reserved for external executor")
        if not (row.get("lot_team") or "").strip():
            reasons.append("project/team missing")
        if backlog._hold_blocks(row, (holds or {}).get(int(row["id"])), stamp):
            reasons.append("pilot hold")
        start = str(row.get("planned_start") or "")[:10]
        if start and start > today:
            reasons.append("planned start: %s" % start)
        if row.get("pending_decision") or row.get("decision_milestone"):
            reasons.append("decision pending")
        if row.get("footprint_overlap"):
            reasons.append("footprint overlaps an occupying lot"
                           + (" (unknown footprint: whole repository)"
                              if "." in (row.get("footprint_paths") or ()) else ""))
        elif row.get("repository_cap"):
            reasons.append("repository in-flight cap reached (%s lots in progress)"
                           % (row.get("repository_in_flight") or "?"))
        if set(row.get("required_capabilities") or ()) & set(backlog.RESERVED_CAPABILITIES):
            reasons.append("human/production capability required")
        reason = spec_reason(row, rule)
        if reason:
            reasons.append(reason)
        for kind, key in (("depends_on", "blocking_dependencies"),
                          ("conflicts_with", "open_conflicts")):
            if row.get(key):
                reasons.append("%s: %s" % (kind, ", ".join("#%s" % i for i in row[key])))
        if not reasons and can_take is not None and not can_take(row):
            reasons.append("not takeable by this dispatcher")
        entries.append(dict(row, ready=not reasons, waiting=reasons))
    bugs = sorted(int(r["id"]) for r in entries if r["ready"] and r["type"] == "bug")
    if bugs and phase:
        for row in entries:
            if row["type"] != "bug":
                row["waiting"].append("bug phase: %s" % ", ".join("#%s" % i for i in bugs))
                row["ready"] = False
    # Topological order first; among the available nodes use the dispatch key.
    pending = {int(r["id"]): r for r in entries}
    ordered = []
    while pending:
        roots = [r for r in pending.values()
                 if not set(r.get("blocking_dependencies") or ()) & pending.keys()]
        row = min(roots or list(pending.values()), key=lambda r: (not r["ready"], order_key(r)))
        pending.pop(int(row["id"]))
        ordered.append(row)
    _disjoint_selection(ordered, policies)
    return {"schema": SCHEMA, "project": project,
            "phase": "bugs" if bugs else "evolutions", "dispatchable_bugs": bugs,
            "ready": [int(r["id"]) for r in ordered if r["ready"]], "lots": ordered}


def acceptance_rule(cfg, project):
    """Invalid settings block their project rather than aborting dispatch."""
    settings = getattr(cfg, "project_planning", {})
    if not isinstance(settings, dict):
        return "invalid project_planning"
    entry = settings.get(project, {})
    if not isinstance(entry, dict):
        return "invalid project_planning entry"
    return entry.get("spec_acceptance")


def report(db, project, *, cfg=None, now=None, can_take=None, phase=True, snapshots=None):
    from . import backlog
    cfg = cfg or getattr(db, "cfg", None)
    rule = acceptance_rule(cfg, project)
    if snapshots is not None and project in snapshots:
        rows, holds = snapshots[project]
    else:
        rows = [dict(row) for row in storage.of(db).work.project_plan_rows(project)]
        holds = backlog._take_holds(db, rows, cfg=cfg)
        if snapshots is not None:
            snapshots[project] = (rows, holds)
    from . import work_footprints
    policies = work_footprints.policies_of(db, {str(r.get("app") or "") for r in rows})
    return evaluate(rows, project=project, rule=rule, holds=holds, now=now,
                    can_take=can_take, phase=phase, policies=policies)


def eligible(db, items, *, cfg=None, now=None, can_take=None, phase=True, snapshots=None):
    """Use the full project backlog, never a truncated candidate window."""
    reports = {p: report(db, p, cfg=cfg, now=now, can_take=can_take, phase=phase, snapshots=snapshots)
               for p in {str(i.get("app") or i.get("lot_team") or "").strip() for i in items}}
    ready = {i for r in reports.values() for i in r["ready"]}
    return sorted((i for i in items if int(i["id"]) in ready), key=order_key)


def assignment_ready(db, item, done, *, cfg=None, now=None, can_take=None):
    """Recheck under the assignment's row lock; our own assignment is ignored."""
    from . import backlog
    project = str(item.get("app") or item.get("lot_team") or "").strip()
    rows = storage.of(db).work.project_plan_rows(project, assignment_id=int(done["id"]))
    target = next((r for r in rows if int(r["id"]) == int(done["id"])), None)
    if target is None:
        return False
    target["assignee"] = None
    target["returned_to_queue"] = item.get("returned_to_queue", False)
    cfg = cfg or getattr(db, "cfg", None)
    rule = acceptance_rule(cfg, project)
    holds = backlog._take_holds(db, rows, cfg=cfg)
    from . import work_footprints
    policies = work_footprints.policies_of(db, {str(r.get("app") or "") for r in rows})
    return int(done["id"]) in evaluate(rows, project=project, rule=rule, holds=holds, now=now,
                                       can_take=can_take, policies=policies)["ready"]


def mark_taken(snapshots, item_id):
    """Reflect this pass's takes without rereading every project for each agent."""
    for rows, _holds in snapshots.values():
        for row in rows:
            if int(row["id"]) == int(item_id):
                row["assignee"] = "taken"
