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
