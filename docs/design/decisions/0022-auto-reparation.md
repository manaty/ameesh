---
type: Decision
title: "ameesh automatically detects and repairs the bugs raised by interactions"
description: "Complete loop signals → triage → repair → verification → delivery → monitoring, governed by a self-repair policy in the canon, with no implicit approval for the irreversible."
status: stable
tags: [auto-reparation, signaux, triage, gouvernance]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T03:05:00+02:00" }
sources:
  - { resource: "../etudes/boucle-auto-reparation.md", title: "Study of the self-repair loop" }
---

# Decision

ameesh must **automatically detect and repair the bugs raised by
interactions**, on the model of a ticket orchestrator already in
production, keeping what works and correcting its limits
([study](../etudes/boucle-auto-reparation.md)).

# Consequences

- Loop: **signals** (errors, alerts, conversations, support, user
  feedback) → **classification and deduplication** → **triage** according to the
  **canon's self-repair policy** (per team: sources, kinds,
  severities and paths repairable unattended, budgets, durable kill switch) →
  **repair** by an agent (reproduce first, smallest fix, tests,
  report, guards on the diff) → **verification** with evidence → **delivery**
  through the gate (the merge is an action; promotion to production remains
  human by default) → **post-delivery monitoring** and **reopening on
  recurrence** → notification of the requester.
- Intended differences from the studied model: every human decision goes through
  a signed message or a receipt, **never through free text or a reaction**;
  **no implicit approval by silence for an irreversible
  or costly action** (escalation changes the recipient, it does not decide);
  logged transitions; durable state and several runners (leases);
  switches in the canon.
- Feature changes are never implemented automatically without a human
  decision; data corrections are proposed, never executed unattended.
- Requirement R23; lots L18 to L23 (specification §13).
