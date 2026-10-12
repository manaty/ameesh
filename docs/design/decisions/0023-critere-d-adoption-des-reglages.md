---
type: Decision
title: "A setting is adopted only for a significant gain, with no loss of overall quality"
description: "Adoption criterion for a model, an effort or a process by the evaluation service: significant gain in lead time or cost, overall quality measured after review and acceptance testing unchanged or better."
status: stable
tags: [evaluation, qualite, modeles, processus]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "owner criterion passed on by the Nexlink orchestrator (message of 04:53), reported by mesh-design; unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T05:00:00+02:00" }
sources:
  - { resource: "../etudes/constats-modeles-2026-10.md", title: "Findings and first isolated replay" }
---

# Decision

A **model**, **effort** or **process** setting is adopted only if it
brings a **significant gain** (lead time or cost), and **never at the expense of
overall quality**, measured **after review and acceptance testing**, which must remain optimal.

# Consequences

- The evaluation metric (lots L15 and L17) is not "green tests" but:
  the reviewer's verdict, defects found in review and after merge, number
  of blocked round trips, defects in acceptance testing; alongside lead time and cost.
- A replay only counts if it is isolated from any trace of the solution.
- Immediate application: DeepSeek's low effort for implementation is
  ruled out for now (no time gain, one quality point lost).
