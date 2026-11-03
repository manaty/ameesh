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
Reservation keys are those of the live dispatcher (below): `project:<app>`
and `class:<priority>`; a bare name still means a project or repository.
In `--project` mode a host without `reservations` in the file takes those of
its Host records. Unreserved slots are shared. Among eligible lots admission
uses ascending priority class, then creation time, then ID. Existing runs are placed first;
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
`completed_ts`; no run slot is retained. The caller owns event subscription
and publication of the JSON. This command does not register a background
subscriber.

For an unmet `deadline_ts`, the forecast tries adding unreserved slots on an
additional host and returns the smallest tested feasible capacity. This is a
feasible greedy list schedule, not a proof of optimality. Added slots cannot
speed a serial dependency, gate, footprint conflict, or host-pinned run. A
null `required_slots` includes an explanation; adding capacity to a specific
existing host can be evaluated by changing its `slots` explicitly. The
critical chain describes this allocation, rather than a resource-free CPM
path. Calendar dates and epic delivery bounds inherit these assumptions.

## Live reservations (#5601)

A Host record may keep some of its `policy.max_agents` places for a project
or a priority class:

```yaml
policy:
  max_agents: 6
  slot_reservations: {"project:nexlink": 2, "class:1": 1}
```

An agent fills a reservation when its next lot (the one it would wake for:
highest priority, then oldest) belongs to that project (`app`) or has that
priority. A free place goes to another agent only while the unfilled
reservations still fit in the places left. With reservations on its host,
the runner admits unleased agents by the class, then the creation date of
their next lot; elected roles come first, agents without an actionable lot
last. Without reservations the claim order is unchanged and no lot is read.
Reservations divide the `max_agents` of the runner. More reserved places than
`max_agents` is a canon error; an unreadable entry is a canon error too and
is ignored, so it never holds places nobody can take.
