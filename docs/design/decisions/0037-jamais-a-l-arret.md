---
type: Decision
title: "ameesh never stops, it improves"
description: "A continuous improvement backlog, made of lots with an expected value, is picked up automatically by idle agents when no lot is waiting for them; never an irreversible or production action, plans first, bounded rate; an alert when the backlog is empty."
status: proposed
tags: [exploitation, sous-utilisation, lots, amelioration-continue]
decided_by: human:smichea
decision_date: 2026-10-10
attestation: "owner request in his conversation with mesh-design; unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-10T21:30:00+02:00" }
sources:
  - { resource: "0036-auditeur-interne.md", title: "Internal auditor" }
  - { resource: "0034-consommer-d-abord-ce-qui-expire.md", title: "Consume first what expires" }
  - { resource: "0030-pas-de-travail-sans-reveil-possible.md", title: "No work without a possible wake-up" }
---

# Context

L94 raises alerts on under-use: plan lost at the reset,
wakeable agents idle while work is waiting. But when nothing
is waiting, the alert goes quiet and the plan agents sleep: the capacity
paid in advance is lost ([0034](0034-consommer-d-abord-ce-qui-expire.md)).
The owner wants ameesh to be "never stopped": an idle agent
must find by itself something useful to do, without a
human having to hand it over, and without it becoming busywork
or a risk to production.

# Decision

1. **A continuous improvement backlog.** An item is a lot
   (`work_items`) of type `improvement`, in `intake`, with no assignee. It carries
   an **expected value** in one sentence and a **score** from 1 to 100, both
   mandatory (rejected by the CLI and by the database): no value, no
   item. Added to that are a priority (1 high, 2 normal, 3 low), the
   source (finding of the auditor [0036](0036-auditeur-interne.md), flaky
   test, debt, recurring alert…), a team and required capabilities.
   No new table: the lots' lifecycle, journal, milestones, progress view and alerts
   apply as is. `ameesh work backlog add|list`.
2. **Automatic pickup** at each run of `ameesh notify`: a wakeable
   agent idle for `--take-idle` (30 min) takes the best-ranked
   item (priority, then value, then age) that matches its team
   and its capabilities. The lot is assigned to it through guarded assignment
   ([0030](0030-pas-de-travail-sans-reveil-possible.md)) and announced by a
   mail linked to the lot, which wakes it up.
3. **Only if no lot is waiting for it.** The agent has no open lot, no
   unread mail, no pending instruction, and no session lot still open. And
   as long as a lot of the project is waiting for a taker (open, with no assignee, outside the
   backlog), nobody takes an improvement: a human's request comes
   first, and it is up to the orchestrator or the human to hand it over (`idle_capacity`
   reports it).
4. **Never an irreversible or production action.** The pickup mail
   forbids it explicitly (deployment, merge, data deletion,
   production server, committed spending): the agent proposes it to a human
   and stops there. The project's circuit (review, integration by whoever has
   the right) remains the only path to production. An item that has lost
   its value is given back (`ameesh work close --abandoned`), not worked on as
   busywork.
5. **What costs money, bounded.** Plan agents first; a pay-per-token
   agent takes nothing unless `--take-paid`, and never during `balance_low`.
   No pickup if a budget cap is reached, if an agent of the mesh is
   on budget pause, nor on a host under pressure, on low battery or not
   ready. At most `--take-max-per-hour` pickups (2) per sliding hour, counted
   in the database for the whole mesh. Assignment is conditional: two
   concurrent takers do not take the same item. *Amended on 2026-10-13, see
   below: the `--take-paid` gate and the mesh-wide budget pause are removed.*
6. **Empty backlog, alert.** `backlog_empty` is raised when wakeable agents
   are idle and the backlog has nothing left to take (and
   no lot of the project is waiting); `ameesh notify` pushes it to the human
   in charge, invited to fill the backlog.

# Consequences

- An item in the backlog is neither an "unassigned lot" of `idle_capacity`, nor a
  stagnant lot: it is waiting for its taker.
- The core stays generic: nothing names a business domain; an item's source
  is free text.
- The internal auditor (0036) is the natural source of the backlog: its findings
  that do not call for a reversible action become items with an expected
  value.
- `--take-idle 0` (or `AMEESH_TAKE_IDLE=0`) turns pickup off; `--dry-run`
  shows the pickups that would be made without assigning anything.

# Amendment of 2026-10-13: what the plans leave goes to the token

Owner instruction of 2026-10-13 (canon, *Processus de lot* §4): "Quand les
forfaits Claude sont au seuil, le travail passe à DeepSeek et Codex. […] un
agent DeepSeek libre face à une file non vide est un dysfonctionnement."
Lot #754, P1.

## Amended rule (§5)

**Plan agents first, then the token in the same pass.** The candidates are
sorted plans first; each takes the best item or lot that fits it, and the
pay-per-token agents then take what is left, immediately, never during a
`balance_low` alert.

Two consequences:

- the `--take-paid` gate disappears, along with the mesh-wide budget pause:
  a free pay-per-token agent facing a non-empty queue is a dysfunction, and
  a plan agent at its ceiling must not stop the others. An agent on budget
  pause takes nothing itself; it makes no one else wait;
- a plan agent of the same team that cannot take an item (`within_budget`,
  `may_take`, `lot_fits`) or that already took one in the pass no longer
  blocks the token agents: the remaining items go in the same pass.

`--take-idle 0`, `--take-lot-idle 0` and `--dry-run` keep their meaning; a
per-agent cap (`CostBook.over`) and a host under pressure, on low battery or
not ready still stop the pickup.

# Amendment of 2026-10-13 (#804): the executor triggers the take

Canon (*Processus de lot* §4) makes the free agent the one who takes the next
ready lot of its team, without waiting for the pilot. The take of L119 and
L165 was in practice only triggered by `ameesh notify`, and no `notify`
process or unit was running on the agent hosts (constat of 13/10 08:0x): idle
agents took nothing. Lot #754 fixed what the take refused (budget pause,
token agents); #804 fixes who triggers it.

**The runner triggers the take for its own agents.** Every
`--take-interval` seconds (default 60, `AMEESH_TAKE_INTERVAL`; 0 turns the
runner's take off), each executor calls the same `backlog.auto_take` for the
agents it serves, on the host where they run, with the same safeguards
(idleness, budget, hosts on hold, hourly caps counted in the database):

- only its own agents are takers (`agents=` filter); the mesh listing stays
  whole for the guards and for notifying the orchestrators;
- a host under pressure is passed as a local `host_pressure` alert, as
  `ameesh notify` does with its alerts;
- the take happens before the executor claims its agents, so the taken lot
  and its mail are served in the same pass;
- a failing take pass never takes the executor down: it is logged and the
  next pass retries.

`ameesh notify` keeps its own take for hosts where it runs; the two share the
database counters, so the mesh-wide caps hold.
