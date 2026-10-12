---
type: Decision
title: "Budgets and routing: model and effort per task, caps, plan gauges read at the source"
description: "The orchestrator chooses model and effort per task; budgets per harness, agent and project in the canon; hourly cap for pay-per-token usage; plans never exceeded, read at their source; accounting per turn."
status: stable
tags: [budget, cout, forfait, routage, modele]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "owner decision passed on by the Nexlink orchestrator (message of 00:18), reported by mesh-design; unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T02:30:00+02:00" }
sources:
  - { resource: "~/.local/bin/nexlink-cost", title: "Reference implementation (accounting, budgets)" }
  - { resource: "~/.local/bin/nexlink-agent", title: "Reference implementation (set, budget_gate, interrupt)" }
---

# Decision

1. **Routing**: the orchestrator chooses and changes the **model** and the **effort**
   of each agent according to the task (strong model for security and native,
   low effort for texts and tests); takes effect at the next turn.
2. **Budgets**: for **pay-per-token** usage, a cap per sliding hour
   (current reference: 10 USD/h for all DeepSeek agents together). For
   **plans** (Claude, Codex): **never exceed their limits**. Pacing
   safeguard: pause a harness's agents if usage reaches
   `min(90 %, elapsed share of the window + 10 points)`, read from the **real
   gauges** (Claude: `rate_limit_event` of the stream-json, five-hour and
   seven-day windows; Codex: primary and secondary `rate_limits` from the
   session logs).
3. **Accounting per turn**: difference of cumulative cost per session (Claude),
   of cumulative usage per thread (Codex), usage per step × rate card (DeepSeek);
   editable rate card.
4. **The orchestrator and the humans count against the same plan** as the
   agents of the same harness (hence the 10-point margin).

# Consequences

- Requirement R20; in the canon: budgets per harness, per agent and per project,
  rate card, model/effort routing rules per task and risk class.
- In ameesh: gauge readers and accounting per turn (lot L12, outside the
  runner); budget guard before each turn and application of the model and
  effort per harness (lot L13, in the runner, after L8).
- `ameesh cost` replaces `nexlink-agent cost`; `ameesh set <agent>
  model=… effort=…` writes the execution state (the canon holds the default
  values and the caps).
