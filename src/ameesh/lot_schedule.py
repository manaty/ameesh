# SPDX-License-Identifier: AGPL-3.0-only
"""Planned dates of the open lots from the reference table (lot L157c).

  ameesh work schedule [--assignee A] [--apply] [--force] [--json]

Point 5 of L157b (#208): planned dates are set at the p80 of the reference
table, lots are ordered at the median. For each agent, its open lots form a
queue:

1. **Order**: priority 1 first, then lots in progress (by start), then by
   priority (1 high), by planned start already set, by number.
2. **Duration of a lot**: its reference group in the table published by the
   auditor (`estimates.lookup`: type, size, host and harness of the
   assignee — median and p80); without a group, the lot's own estimate
   (median = p80 = estimate); without either, the lot is left unplanned and
   does not move the queue.
3. **Chaining at the median**: the next lot starts when the previous one is
   expected to end at its MEDIAN (a lot in progress: its median minus the
   time already spent, never less than now). The queue moves at the
   expected pace, not at the cautious one.
4. **Dates at the p80**: a lot's planned start is the day its turn comes;
   its planned end is the day of start + p80 (a lot in progress: the day of
   its measured start, and of max(now, start + p80)). The promised date is
   the cautious one. Days are LOCAL days, as everywhere in the roadmap
   (`roadmap.today`).
5. **#5151**: a lot waiting on someone else (`qa`, `validated`,
   `waiting_human`, `blocked`) gets its dates but does not move the queue;
   a priority 1 lot is placed before the lower priority ones, even in
   progress (whose remaining time moves after it).
6. **A lot dated by a person** keeps its date and only takes its place in
   the queue from that date (no earlier than the queue reaches it): a P1
   lot set for next month does not push back the lots after it.

Only reads: the published table, the registry and the open lots — no
analysis of past lots (the owner's rule, L157b). Without `--apply`, nothing
is written. `--apply` writes the dates of the lots without planned dates or
whose dates came from this schedule (`planned_source` starting with
`SOURCE`); dates set by a person are kept (`--force` overwrites them) and
their lot still takes its place in the queue. A lot is written only while
its dates are still those the schedule read (#740): a date set while the
schedule was being applied stays.

`--pin` (#740): a date set by hand before #704 still carries `SOURCE`, and
nothing stored tells it from a date the schedule wrote. Without ids, the
lots dated with `SOURCE` and who dated them (dry run); with ids, a human,
an orchestrator or a design agent keeps those lots' dates (`set by <who
dated them>`).

`work add --assignee A` schedules the new lot this way when a reference
table is published.
"""
from __future__ import annotations

import datetime as _dt
import time

from . import estimates, storage
from . import work as work_mod

SCHEMA = "ameesh-schedule/1"
#: `planned_source` of the dates written by the schedule
SOURCE = "reference p80"
#: `planned_source` of dates a person set, or cleared, on purpose (#704, #740)
MARKER = "set by "
#: open lots read at most per state
LOT_LIMIT = 1000
#: priority of a lot without one (normal)
DEFAULT_PRIORITY = 2


def _day(ts: float) -> str:
    return _dt.date.fromtimestamp(float(ts)).isoformat()


def open_states() -> tuple:
    return tuple(s for s in work_mod.STATES
                 if s not in work_mod.MERGED_STATES and s != "closed")


def waits_on_others(lot: dict) -> bool:
    """#5151: a lot waiting on someone else than its assignee (review,
    package or merge, decision, block — `WAITING_ON_OTHERS`, as the
    `active_lots` of `operations.listing`, L165b) does not hold the queue."""
    from . import work_lifecycle
    return lot.get("state") in work_lifecycle.WAITING_ON_OTHERS


def _p1(lot: dict) -> bool:
    return int(lot.get("priority") or DEFAULT_PRIORITY) == 1


def queue_order(lot: dict, plan: dict | None):
    """Sort key of a queue: priority 1 first (#5151: a P1 never waits behind
    a lower priority lot, even one in progress), then in progress (by
    start), priority, planned start already set, number."""
    started = lot.get("started_ts")
    planned = (plan or {}).get("planned_start")
    return (0 if _p1(lot) else 1, 0 if started is not None else 1, float(started or 0),
            int(lot.get("priority") or DEFAULT_PRIORITY), str(planned or "9999-99-99"),
            int(lot["id"]))


def duration(lot: dict, body: dict | None, agent: dict | None) -> dict | None:
    """`{median_minutes, p80_minutes, basis, …group keys}` of a lot, or None."""
    agent = agent or {}
    group = estimates.lookup(body, type=lot.get("type"), size=lot.get("change_size"),
                             host=agent.get("host"), harness=agent.get("harness")) \
        if body else None
    if group:
        return {"median_minutes": float(group["median_minutes"]),
                "p80_minutes": float(group["p80_minutes"]),
                "basis": "reference", "level": group.get("level"),
                "group_lots": int(group.get("lots") or 0)}
    if lot.get("estimate_minutes"):
        minutes = float(lot["estimate_minutes"])
        return {"median_minutes": minutes, "p80_minutes": minutes, "basis": "estimate",
                "level": None, "group_lots": None}
    return None


def pinned_ts(lot: dict, plan: dict | None, force: bool = False) -> float | None:
    """The start (local midnight) of a lot not started whose planned start
    was set by a person, None otherwise (or with `force`)."""
    from . import plan as plan_mod

    plan = plan or {}
    if force or lot.get("started_ts") is not None or not plan.get("planned_start"):
        return None
    if str(plan.get("planned_source") or "").startswith(SOURCE):
        return None
    return plan_mod.day_start_ts(str(plan["planned_start"])[:10])


def plan_queue(lots: list[dict], plans: dict, body: dict | None, agent: dict | None, *,
               now: float, force: bool = False) -> list[dict]:
    """The schedule of ONE agent's queue (pure): one entry per lot, in the
    order the lots are placed, `{id, title, state, start_ts, end_ts,
    planned_start, planned_end, median_minutes, p80_minutes, basis, pinned,
    current}` (`current`: the dates already set).

    A lot dated by a person (`pinned_ts`) keeps its date and only takes its
    place in the queue from that date: the lots after it in the order go
    first while the queue has not reached it."""
    now = float(now)
    cursor = now
    out = []
    ordered = sorted(lots, key=lambda l: queue_order(l, plans.get(int(l["id"]))))
    pins = {int(l["id"]): pinned_ts(l, plans.get(int(l["id"])), force) for l in ordered}
    normal = [l for l in ordered if pins[int(l["id"])] is None]
    pinned = sorted((l for l in ordered if pins[int(l["id"])] is not None),
                    key=lambda l: pins[int(l["id"])])
    while normal or pinned:
        if pinned and (not normal or pins[int(pinned[0]["id"])] <= cursor):
            lot = pinned.pop(0)
        else:
            lot = normal.pop(0)
        pin = pins[int(lot["id"])]
        plan = plans.get(int(lot["id"])) or {}
        entry = {"id": int(lot["id"]), "title": lot.get("title") or "",
                 "state": lot.get("state"), "assignee": lot.get("assignee"),
                 "pinned": pin is not None,
                 "current": {"planned_start": plan.get("planned_start"),
                             "planned_end": plan.get("planned_end"),
                             "planned_source": plan.get("planned_source"),
                             "planned_by": plan.get("planned_by")}}
        d = duration(lot, body, agent)
        if d is None:
            entry.update(basis=None, start_ts=None, end_ts=None, planned_start=None,
                         planned_end=None, median_minutes=None, p80_minutes=None)
            out.append(entry)
            continue
        entry.update(d)
        median_s, p80_s = d["median_minutes"] * 60.0, d["p80_minutes"] * 60.0
        started = lot.get("started_ts")
        aside = waits_on_others(lot)
        if started is not None:
            started = float(started)
            if aside:
                start, end = started, max(now, started + p80_s)
            else:
                # behind a P1 placed first (#5151), its rest moves by as much
                base = max(cursor, now)
                start = started
                end = max(now, started + p80_s, base + max(0.0, started + p80_s - now))
                cursor = base + max(0.0, started + median_s - now)
        else:
            start = now if aside else cursor
            if pin is not None:
                start = max(start, pin)
            end = start + p80_s
            if not aside:
                cursor = start + median_s
        entry.update(start_ts=round(start, 3), end_ts=round(end, 3),
                     planned_start=_day(start), planned_end=_day(end))
        out.append(entry)
    return out


def _writable(entry: dict, force: bool) -> bool:
    if entry.get("planned_start") is None:
        return False
    cur = entry["current"]
    if (cur.get("planned_start"), cur.get("planned_end")) == (entry["planned_start"],
                                                              entry["planned_end"]):
        return False
    if force:
        return True
    source = cur.get("planned_source") or ""
    if source.startswith(SOURCE):
        return True
    # a lot without dates is the schedule's, unless a person cleared them on
    # purpose: its source then says who (#740)
    return not cur.get("planned_start") and not cur.get("planned_end") \
        and not source.startswith(MARKER)


def build(db, *, assignee: str | None = None, now: float | None = None,
          force: bool = False) -> dict:
    """The schedule of the open assigned lots (of `assignee`, or all):
    `ameesh-schedule/1`. Reads only."""
    from . import registry

    now = time.time() if now is None else float(now)
    ref = estimates.current_reference(db)
    body = ref["body"] if ref else None
    st = storage.of(db)
    lots = []
    for state in open_states():
        lots += [l for l in st.work.items(state=state, assignee=assignee, limit=LOT_LIMIT)
                 if l.get("assignee")]
    plans = {int(p["id"]): p for p in st.roadmap.item_plans([int(l["id"]) for l in lots])}
    queues: dict = {}
    for lot in lots:
        queues.setdefault(lot["assignee"], []).append(lot)
    agents = {}
    for name in queues:
        try:
            agents[name] = registry.get(db, name) or {}
        except Exception:  # noqa: BLE001 - unreadable registry: wider group
            agents[name] = {}
    out_queues = []
    for name in sorted(queues):
        entries = plan_queue(queues[name], plans, body, agents[name], now=now, force=force)
        out_queues.append({"assignee": name, "lots": entries})
    return {"schema": SCHEMA, "generated_ts": round(now, 3),
            "reference": {"id": ref.get("id"), "published_ts": ref.get("published_ts"),
                          "published_by": ref.get("published_by")} if ref else None,
            "queues": out_queues}


def apply(db, schedule: dict, *, force: bool = False, only=None, actor: str = "") -> list[dict]:
    """Writes the planned dates of the schedule (see the module doc). `only`:
    these lots only. Returns the entries written."""
    st = storage.of(db)
    ref = schedule.get("reference") or {}
    written = []
    for queue in schedule["queues"]:
        for entry in queue["lots"]:
            if only is not None and entry["id"] not in only:
                continue
            if not _writable(entry, force):
                continue
            source = "%s (%s)" % (
                SOURCE, "table #%s" % ref["id"] if entry["basis"] == "reference" and ref
                else "lot estimate")
            row = st.roadmap.plan_item(entry["id"], {"start": entry["planned_start"],
                                                     "end": entry["planned_end"]},
                                       source=source, actor=actor, expect=entry["current"])
            if row is not None:     # None: changed since the schedule read it (#740)
                written.append(dict(entry, source=source))
    return written


def _dated_by_schedule(plan: dict) -> bool:
    return bool(plan.get("planned_start") or plan.get("planned_end")) \
        and str(plan.get("planned_source") or "").startswith(SOURCE)


def pin_candidates(schedule: dict) -> list[dict]:
    """#740: the lots of the schedule whose dates carry `SOURCE` (written by
    the schedule, or set by hand before #704), with who dated them and
    whether the next `--apply` would move them. Reads only."""
    return [{"id": e["id"], "title": e["title"], "state": e["state"],
             "assignee": queue["assignee"], "planned_start": e["current"]["planned_start"],
             "planned_end": e["current"]["planned_end"],
             "planned_source": e["current"]["planned_source"],
             "planned_by": e["current"].get("planned_by"), "moves": _writable(e, False)}
            for queue in schedule["queues"] for e in queue["lots"]
            if _dated_by_schedule(e["current"])]


def pin(db, ids, *, actor: str = "") -> list[dict]:
    """#740: keeps the dates of these lots from the schedule: their source
    becomes `set by <who dated them>`. Only a lot dated with `SOURCE`, and
    only if it did not change since it was read. WorkError unless the actor
    is a human, an orchestrator or a design agent by a declared role
    (`work.corrector` without the assignment history, as `work.pin_authority`).
    Returns `{id, status, …}` per lot: pinned, skipped, changed or unknown."""
    actor = work_mod.corrector(getattr(db, "cfg", None), db, actor, history=False)
    st = storage.of(db)
    plans = {int(p["id"]): p for p in st.roadmap.item_plans([int(i) for i in ids])}
    out = []
    for lot in (int(i) for i in ids):
        plan = plans.get(lot)
        if plan is None:
            out.append({"id": lot, "status": "unknown"})
        elif not _dated_by_schedule(plan):
            out.append({"id": lot, "status": "skipped",
                        "reason": "not dated by the schedule (source: %s)"
                                  % (plan.get("planned_source") or "none")})
        else:
            source = MARKER + (plan.get("planned_by") or actor)
            row = st.roadmap.plan_item(lot, {}, source=source, actor=actor, expect=plan)
            out.append({"id": lot, "status": "pinned", "source": source} if row is not None
                       else {"id": lot, "status": "changed",
                             "reason": "changed since it was read: run it again"})
    return out


def schedule_new(db, item: dict, *, actor: str = "", now: float | None = None) -> dict | None:
    """`work add --assignee`: plans the new lot in its assignee's queue when
    a reference table is published. Returns its entry, or None. Never
    raises (the lot is created anyway)."""
    if not item.get("assignee"):
        return None
    try:
        if estimates.current_reference(db) is None:
            return None
        sched = build(db, assignee=item["assignee"], now=now)
        written = apply(db, sched, only={int(item["id"])}, actor=actor)
        return written[0] if written else None
    except Exception:  # noqa: BLE001 - planning is a help, never a refusal
        return None


def format_schedule(schedule: dict, written: list[dict] | None = None) -> str:
    ref = schedule.get("reference")
    out = ["schedule — %s" % (
        "reference table #%s (%s)" % (ref["id"], ref.get("published_by") or "?") if ref
        else "no reference table published: lot estimates only")]
    done = {e["id"] for e in written or ()}
    for queue in schedule["queues"]:
        out.append("")
        out.append(queue["assignee"])
        for e in queue["lots"]:
            if e["planned_start"] is None:
                out.append("  #%-5d %-12s unplanned (no reference group, no estimate)  %s"
                           % (e["id"], e["state"], e["title"][:50]))
                continue
            cur = e["current"]
            kept = ""
            if e["id"] not in done and written is not None and (
                    cur.get("planned_start") or cur.get("planned_end")) and (
                    cur.get("planned_start"), cur.get("planned_end")) != (
                    e["planned_start"], e["planned_end"]):
                kept = "  (kept: %s → %s)" % (cur.get("planned_start") or "—",
                                              cur.get("planned_end") or "—")
            out.append("  #%-5d %-12s %s → %s  median %s · p80 %s (%s)%s%s  %s" % (
                e["id"], e["state"], e["planned_start"], e["planned_end"],
                estimates.label(e["median_minutes"]), estimates.label(e["p80_minutes"]),
                e["basis"], "  written" if e["id"] in done else "", kept, e["title"][:50]))
    if not schedule["queues"]:
        out.append("no open assigned lot")
    return "\n".join(out)
