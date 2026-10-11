---
type: Decision
title: "No work without a possible wake-up"
description: "A lot, a delegation or an orchestrator role is only entrusted to an agent that ameesh knows how to wake up; adoption and resumption become ameesh operations; liveness alerts are pushed to the human in charge; identity never comes from the directory."
status: stable
tags: [orchestrateur, bail, sessions, alertes, delegation, cloisonnement]
decided_by: human:smichea
decision_date: 2026-10-07
attestation: "owner decision in his conversation with claude3 (mesh-design); unsigned"
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-07T13:00:00+02:00" }
---

# Context

On 2026-10-07, a project stopped silently. Its
orchestrator was an interactive Codex session, with no lease and no
human in charge. ameesh could neither wake it up, nor deliver its
mail, nor report that it had stopped. Full analysis:
[orchestration and liveness](../etudes/orchestration-et-vivacite.md).

# Decision

1. **Explicit agent mode**: `exécuté` (driven by a runner, under lease) or
   `externe` (human session, mailbox only). An external agent
   must have a human in charge.
2. **Guarded assignment**: a lot, a delegation or an orchestrator role only
   goes to a wakeable agent (known, `exécuté`, admitted on a host whose
   runner is alive). Only the human in charge can force an assignment to
   an external agent; they then become the recipient of the lot's alerts.
   Extends [0014](0014-orchestrateurs-a-tours-et-placement.md).
3. **Adoption and resumption are ameesh operations** (`ameesh adopt`,
   `ameesh resume`), never prompts. A session's original account is
   recorded. If that account is at the threshold, the rule of
   [0027](0027-bascule-automatique-entre-comptes.md) applies; if the old
   account is unusable, ameesh builds a deterministic resume brief.
4. **Liveness alerts pushed** to the human in charge of the agent or the lot:
   `stopped_with_mail` (except manual stop, with a structured stop
   reason), `orphan_lot`, `delegation_expired`.
5. **Delegation with a deadline**: with no turn by the delegate by the deadline, the lot returns
   to the delegator.
6. **Identity never comes from the directory**: hooks move to v1, an
   external session is bound explicitly by its harness's session
   identifier, and nothing is delivered without a binding. `send all` is limited to the
   sender's project.

# Consequences

- Lots L36 to L41 ([study](../etudes/orchestration-et-vivacite.md#proposed-lot-plan));
  migrations 0030 and 0031 reserved.
- Order: L36, then L37, then L38, L39 and L41 in parallel, then L40. Each
  lot is merged on the owner's approval.
- The orchestrator of the stuck project is restarted by hand by its
  person in charge, pending `ameesh adopt` / `ameesh resume` (L39).
