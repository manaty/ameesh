# Capacity and delivery forecasts

`ameesh work plan --capacity-plan snapshot.json` prints an inspectable JSON
forecast (`ameesh-capacity-plan/1`). It computes UTC start/end times, host and
slot allocation, epic bounds, start waves and a critical chain including
resource/footprint contention. It does not dispatch runs or change saved dates.
AI implementation, review and QA consume run slots; only explicit decisions,
physical checks and external waits add human delays. Durations are elapsed
minutes, with continuous AI availability, rather than human working days.

A standalone snapshot contains the graph and capacity:

```json
{
  "now": 1800000000,
  "hosts": [{"name": "worker", "slots": 2, "reservations": {"acme": 1}}],
  "project_classes": {"acme": 1},
  "approver_minutes": {"product-owner": 120},
  "lots": [
    {"id": 1, "project": "acme", "repository": "acme/app", "epic": "pricing",
     "estimate_minutes": 60, "created_ts": 1, "footprint_paths": ["src/base"]},
    {"id": 2, "project": "acme", "repository": "acme/app", "epic": "pricing",
     "estimate_minutes": 90, "created_ts": 2, "footprint_paths": ["src/pricing"],
     "depends_on": [1], "gates": [{"approver": "product-owner"}]}
  ],
  "history": [{"estimate_minutes": 240, "actual_minutes": 357, "late": false}],
  "deadline_ts": 1800021600
}
```

`depends_on` is directional: a lot lists its prerequisites. Alternatively,
`links` contains `{source, target, kind}` entries: `depends_on`,
`conflicts_with`, `part_of`, `duplicates`, `supersedes`. The last two annotate
rather than block execution; omit already deduplicated/superseded work from
the runnable graph. Unknown prerequisites and cycles are errors. Use the
`epic` field for an epic that is not itself a runnable lot.

The duration multiplier is the median recorded actual/estimated ratio in the
same type/size cohort, falling back to type then all history. Late estimates
are excluded. Every result includes the ratio and sample count. With no
history, the estimate remains unchanged. Existing recorded actuals measure
wall time; if they include human waits, that cohort can overstate AI run
time when explicit gates are added. Use phase-specific standalone history
when available, and inspect the published multiplier before promising dates. Completed lots need no estimate.

To read current project work and actuals directly:

```sh
ameesh work plan --project acme --capacity-plan capacity.json > forecast.json
```

In this mode the file supplies `now`, `hosts`, policies, classes, approver
delays and optional per-lot gate/resource overrides. The database supplies
the open lots, blocking dependencies, footprints, epic links, estimates and
recorded actuals. Per-lot overrides may contain `gates`, `host`, `slot`,
`priority_class` and `not_before_ts`; they cannot replace the live graph or
estimates. External open prerequisites must be included in a standalone
cross-project snapshot. A blocked lot or pending decision needs explicit
wait details; otherwise the forecast fails with the lot number.

Slots reserved for a project or repository cannot be borrowed by another.
Unreserved slots are shared. Among eligible lots admission uses ascending
priority class, then creation time, then ID. Existing runs are placed first;
there is no implicit one-run-per-repository restriction. In-progress lots may
pin `host` and `slot`; their elapsed time is subtracted from the calibrated
duration (at least one second remains when overdue). If existing runs cannot
fit the supplied capacity, fix the snapshot instead of silently delaying them.

Overlapping footprints in the same repository serialize runs, as do explicit
conflicts. Optional `policies` keyed by repository accept `shared_paths` and
`unknown_footprint: "repository"`; without the latter unknown footprints do
not hold the whole repository. Gates do not occupy slots. A gate's
`response_minutes` overrides its approver's delay. An unanswered gate is expected at `requested_ts` plus its response delay
(or prerequisite completion plus that delay if not requested yet), never
earlier than prerequisite completion or now. Time already spent waiting
therefore reduces the remaining wait. An
`answered_ts` removes the forecast wait. Multiple gates may proceed in parallel.

After a merge, gate answer or capacity change, supply the new snapshot (and
advance `now`) and compare against the saved result:

```sh
ameesh work plan --capacity-plan updated.json --previous-plan forecast.json
```

The output names every moved lot and its old/new times. A completed lot uses
`completed_ts`; no run slot is retained. This standalone command does not register a background subscriber or enforce
reservations in the live dispatcher. For automatic publication from live
project inputs, configure the notification pass described below.

For an unmet `deadline_ts`, the forecast tries adding unreserved slots on an
additional host and returns the smallest tested feasible capacity. This is a
feasible greedy list schedule, not a proof of optimality. Added slots cannot
speed a serial dependency, gate, footprint conflict, or host-pinned run. A
null `required_slots` includes an explanation; adding capacity to a specific
existing host can be evaluated by changing its `slots` explicitly. The
critical chain describes this allocation, rather than a resource-free CPM
path. Calendar dates and epic delivery bounds inherit these assumptions.

## Automatic date publication (#5602)

Configure the existing `issues` notification pass on a transactional database
connection with a project settings file:

```json
{"project_planning":{"acme":{"capacity_plan":"/srv/ameesh/acme-capacity.json"}}}
```

The file uses the capacity settings format above (`hosts`, reservations,
gate/resource overrides, approver delays and policies). It is read again on
each `ameesh notify --only issues` pass, including when GitHub projection is
not configured. `now` comes from the pass clock. Update the file atomically
when offered hosts/slots or reservations change. Live project lots, dependencies
and recorded actuals are read from the database. A gate may contain
`decision_id`; its answer timestamp then comes from the current decision for
that lot, rather than a cached `answered_ts` in the file. Overrides for lots
that merged are discarded. Other explicit external gates still use the file.

The pass detects changes in these inputs and recomputes automatically: an
early/late merge, decision answer or host/slot change needs no manual forecast
command. Clock ticks alone leave the published forecast unchanged. Repeating
the same event, or computing the same projection, publishes nothing. Dates
and a UTC timestamped forecast note for each moved lot commit with a durable
checkpoint, so another host/restart does not publish the same update again.
Incomplete inputs leave the previous dates intact and are logged per project.
The latest checkpoint survives event retention; older checkpoints are pruned.

`ameesh work plan --project acme --json` includes `capacity_forecast`; text
mode shows its delivery bound alongside the usual lot dates. Scheduler-owned
start/end dates are updated; dates pinned by a person and manual delivery
dates remain intact. The journal and forecast still expose the computed ETA
when a manual date is retained. This pass publishes forecasts and does not
change dispatch admission or enforce reservations.
