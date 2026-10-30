---
type: Decision
title: "A real-time progress view (Gantt-like), ameesh's native human rendering"
description: "Lots on a timeline (request → freeze → verdict → merge), agents (model, effort, state, task), milestones and budget, fed by ameesh events; in Nexlink, the natural rendering of the project page."
status: stable
tags: [observabilite, gantt, vue, nexlink]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "owner request (05:47) passed on by the Nexlink orchestrator, reported by mesh-design; unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T05:50:00+02:00" }
---

# Decision

ameesh provides a **real-time view of the project's progress**, Gantt-like,
readable on a phone:

1. the **lots** on a timeline (request → freeze → verdict → merge), their state
   (active, in review, blocked, approved, merged) and their number of blocks;
2. the **agents**: model, effort, state (working, idle, paused), duration and
   current task;
3. the **milestones** (production, stores, acceptance testing…);
4. the **budget**: actual spend of pay-per-token usage, plan gauges.

# Consequences

- It is the native human view of `ameesh list` and of the lot lifecycle,
  **fed by ameesh events** (lot transitions, turns, leases,
  costs, review verdicts) rather than reconstructed from git and the board.
- In Nexlink, the project page is its natural rendering (view of the OKF canon plus
  execution state); a third-party front end can build it on the same
  interfaces ([0015](0015-interfaces-des-facades.md)).
- A reference exists for Nexlink (tracking page built by
  the orchestrator on its own data): it serves as a mock-up.
- Lot L24: JSON export of the events and the state (`ameesh progress --json`),
  timeline data model, and a reference view.

# L99 T3 — reader compatibility for approval (#4351)

`validated` is a distinct stored state after `qa`, separate from `merged`.
This compatibility release understands it in transitions, progress, package
and train candidates, carriers and planned landings. A new freeze or verdict
returns an existing validated lot to `qa`.

It does not create validated lots: review verdicts keep writing `qa`, and
manual `qa → validated` is unavailable. The writer follows in a subsequent release,
after staging has established a compatible reader and rollback baseline.
During that transition, package readers still qualify legacy `qa` lots with
the full exact-head review gate.

# L99 T5 — persisted delivery core (#4353)

Implementation of signed owner decision #164165 (29 October 2026,
approval #167): `merged` and `promoted` persist as `delivered`, with the
existing workflow stage and `legacy_state` preserving their distinction.
The dev module owns promotion and correction transitions. Historical
milestones, signed Process artifacts and public GitHub vocabulary keep
their contracts. Migration and rollback are qualified in a disposable
Postgres schema; the storage switch precedes executor restart.
