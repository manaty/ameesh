---
type: Study
title: "Study — electing a pilot when its host stops"
description: "A mesh keeps working when the owner's workstation is off: every governance role has candidates on several hosts, the most reliable one holds the role through the agent lease and its epoch, and a stopped host loses the role in three minutes."
status: accepted
tags: [orchestrateur, bail, election, hotes, gouvernance, bascule]
generated: { by: "bureau/claude-opus-5-5", at: "2026-10-22T23:30:00+02:00" }
sources:
  - { resource: "../../HOST-HANDOFFS.md", title: "Host drain, return and loss recovery (L232e)" }
  - { resource: "orchestration-et-vivacite.md", title: "Study — a stopped orchestrator blocks a project" }
  - { resource: "../decisions/0025-une-session-par-lot.md", title: "Decision 0025 (sessions, resume summary)" }
---

# Electing a pilot when its host stops

## What happened

The owner's workstation (`pc-smichea`) was off from 2026-10-21 12:09 to
2026-10-22 21:54 (Paris). The VMs stayed up, and 17 DeepSeek agents were
active. Still, Nexlink got 0 commits and ameesh got 9, all from a packet
composed before the stop. The reason is that every governance role runs only
on the workstation: the ameesh and Nexlink pilots (orchestrators), mesh-design,
the auditor, the packet composer, the GitHub watch and the Claude/Codex
reviewers. Nothing on the VMs could take over.

L232e does detect a lost host after 180 s, but it does not move its agents.
Recovery needs a proof from the lost host that its executor is stopped and
that its work is published, and a host that is switched off cannot give that
proof. This is right for a coding agent, whose worktree may hold work that is
not pushed yet. It is wrong for a pilot: what a pilot needs is in the mesh
database, the canon and GitHub, not in a local worktree.

## Rule (owner, 2026-10-22)

> « il ne faut pas qu'un mesh s'arrete de travailler si le pc est eteint, il
> faut qu'un autre pilote soit elu parmis les instances restantes, de
> preference celle qui a le moins de chance de s'arreter: a la maniere d'une
> blockhain avec election de leader »

The owner's choices on the four open points:

| Point | Decision |
|---|---|
| Roles | The whole governance: both pilots, mesh-design, auditor, packet composer, GitHub watch, reviewers |
| Placement | The most reliable host holds the role in normal times, not only after a failure. The workstation is a candidate like the others |
| Model family | Claude and Codex are logged in on the agent VMs, so the elected pilot keeps its family. DeepSeek stands in only when no Claude or Codex account is available |
| Mechanism | The agent lease in the mesh database, taken by compare-and-swap, with its epoch as the fencing token. No protocol between hosts. The database remains a single point, handled in a separate lot |

## Design

### 1. A role is an agent with several candidate hosts

A governance role is already an agent (`orchestrateur`, `auditeur`,
`mesh-design`…), and its lease is already the only authorisation to run
(`registry.py`): `claim()` is an atomic `UPDATE … WHERE`, and `renew()` and
`end_turn()` are fenced by `lease_epoch`. What pins it to one host is
placement: a lease can only be taken back by an executor on the same host,
because harness sessions are local files.

The change is an agent-card field:

```yaml
releve: election          # default: hote (today's behaviour)
candidats: [ameesh-agents-1, ameesh-agents-2, manaty-mesh-1, pc-smichea]
```

With `releve: election`, the runner of that agent is installed on every
candidate host. Only the holder of the lease takes turns. The others are on
standby: they run no turns and cost no tokens, and they only watch the lease.

### 2. Who wins: the most reliable candidate

Each candidate gets a score, computed from data the mesh already has:

* **availability**: the share of minutes over the last 7 days with a fresh
  resource sample from the host. A VM is about 100 %, and the workstation
  scores much lower;
* **readiness**: the harness login is valid, the account is neither paused nor
  over its ceiling, the host gate is open, and the canon is in sync. A
  candidate that is not ready cannot be elected;
* **capacity**: the room left under the host's limits, used only as a
  tie-break.

When the lease is free or expired, the best ready candidate takes it. A
candidate that is not the best waits a delay that grows with its rank before
trying (best: 0 s, then 20 s, 40 s…). The best one therefore wins without any
vote, and the compare-and-swap guarantees that only one wins if two try at the
same time.

**Hysteresis.** A holder keeps the role as long as it renews its lease. A
better candidate takes over only through a planned handover: its score has
stayed at least 5 points higher for an hour, and the switch happens at the end
of the holder's turn. A workstation that boots up therefore does not take the
role back from a VM.

### 3. Timings

The timings are the same as L232e. The holder renews every 60 s. The lease
expires after 180 s without renewal, and the next candidate takes it. **Target:
a new pilot within 5 minutes of a host stopping.**

### 4. Fencing: an old pilot that comes back cannot act

Each election increments `lease_epoch`. When the workstation comes back, its
runner fails to renew (`lease expired`), as the registry already does, and
goes on standby. A pilot's effects outside the database must check the epoch
in the same transaction as their database write. A refusal names the current
holder. These effects are: merge light, merge queue, `work assign`, packet
composition and routing of decisions.

### 5. What the new pilot starts from

There is no session transfer. The new holder opens a new session, and its first
turn is a resume briefing built from the database (open lots, mail, alerts,
decisions), the canon and the GitHub state, as in decision 0025. Work left
unpublished in a worktree of the lost host stays there. It is reconciled when
the host returns, through L232e, and never by the new pilot.

### 6. Mechanical roles

The packet composer, the GitHub watch and the CI watches are code
(owner 2026-10-15, canon home#86). They become timers installed on every candidate host,
wrapped by `ameesh role run ROLE -- command`, which runs the command only while
this host holds the role's lease. They follow the same election rules.

### 7. Cost

Standby runners take no turns, so the election itself costs no tokens; it is a
few SQL statements per minute per role. The only token cost is the new pilot's
first turn, a resume briefing of about one ordinary turn, paid once per
election. Claude and Codex accounts logged in on the VMs are the same
subscriptions as on the workstation. They are shared by the existing account
switch, and the ceilings are unchanged: never any overage.

## Lots

| Ref | Lot | Class |
|---|---|---|
| EP1 | `releve: election` and `candidats` on agent cards; claims across candidate hosts for these agents only; the L232e source proof is not required for them | sensitive (placement, SQL: two families) |
| EP2 | Candidate score (availability over 7 days, readiness, capacity), the rank delay before a claim, and hysteresis; `ameesh role status` shows the holder, the epoch and the ranked candidates | sensitive |
| EP3 | Standby runners: install on every candidate, no turns without the lease, resume briefing on election | sensitive (prod paths) |
| EP4 | Fencing of the pilot's effects outside the database by the lease epoch (merge, queue, assign, packet, decision routing) | security (merge gate: two families) |
| EP5 | `ameesh role run`: the packet composer, the GitHub watch and the CI watches as timers gated by the lease | sensitive |
| EP6 | Claude and Codex logins on ameesh-agents-1 and -2 for the candidates; standby placements of the governance roles (owner validates each OAuth) | operations |
| EP7 | Drill on staging: planned stop of the workstation, then a network cut. Pass criteria: new pilot in under 5 minutes, never two holders, the workstation goes back to standby on return. Same drill for the Nexlink pilot | release proof |
| EP8 | Mesh database replica and its failover (the remaining single point) | separate study |

## Not in scope

* Raft or another consensus between hosts: the mesh database already
  serialises claims, so a lease with an epoch gives the same safety without a
  new component. Raft would only protect against losing the database, which is
  EP8.
* Moving coding agents: they keep L232e and its source proof.
