---
type: Decision
title: "ameesh monitors each host's resources and distributes agents accordingly"
description: "Each runner measures its host's memory, swap, CPU and disk; under pressure it holds back new turns, reports orphaned resources and, when the canon allows several hosts for an agent, moves the agent to a less loaded host between two turns."
status: stable
tags: [placement, ressources, exploitation, multi-hotes]
decided_by: human:smichea
decision_date: 2026-10-05
attestation: "owner request in his conversation with mesh-design, after a memory saturation of the workstation (12 GB of swap); unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-05T17:30:00+02:00" }
---

# Context

The v1 placement is declarative (one agent → one host, `max_agents` per host).
Nothing measures the actual load: a workstation saturated its memory (browser,
agents' test suites, a dozen test databases forgotten for 17 hours)
without any tool seeing it or acting on it.

# Decision

1. **Measurement**: each runner periodically reads its host's resources
   (available memory, swap, CPU load, free disk, number of turns in
   progress) and publishes them to the database; `ameesh hosts [--json]` shows
   them, with the short history.
2. **Thresholds in the canon**: a `Host`'s policy carries thresholds (minimum
   available memory, maximum swap, maximum load, minimum disk), with
   cautious default values.
3. **Local back-pressure**: above a threshold, the runner no longer starts
   a new turn (turns in progress finish) and raises an alert; in a
   critical case, it pauses the lowest-priority agents, never in the
   middle of a turn.
4. **Orphaned resources**: the runner attaches to each turn the processes
   and containers it launches (process group, container labels) and
   reports those that outlive their turn or their agent; it deletes nothing
   without explicit action.
5. **Distribution**: a `Placement` can designate several admitted hosts for an
   agent (order of preference); between two turns, an agent moves to a less
   loaded admitted host (lease released then retaken; same session if session
   storage allows it, otherwise rotation with summary, [0025](0025-une-session-par-lot.md)).
   The host policy and the credentials (mode, accounts, [0027](0027-bascule-automatique-entre-comptes.md))
   are still checked on the destination host.

# Consequences

- Lot L31; builds on L3 (placement), L26 (alerts), L30 (accounts) and L25
  (cluster profile, where each pod is a host).
- Thresholds and admitted hosts are declarative and reviewed (canon); the
  measurements are runtime state (database).
