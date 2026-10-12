---
type: Decision
title: "An ameesh service lists, discovers and evaluates models to guide routing"
description: "Models change constantly: ameesh keeps a catalogue of available models, discovers new ones, evaluates them on reference tasks, and derives model and effort recommendations per task class."
status: stable
tags: [modeles, catalogue, evaluation, routage, budget]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T02:45:00+02:00" }
---

# Context

Model/effort routing per task ([0019](0019-budgets-et-routage.md)) assumes
knowing which models exist, what they cost and what they are worth for
each type of task. New models come out constantly.

# Decision

An **ameesh service**:

1. **lists** the usable models, per provider and per harness;
2. **discovers** new ones (and those that disappear or change price);
3. **evaluates** the models and effort levels on reference tasks;

so that **the orchestrators can determine which model and which effort
to use for which task**.

# Consequences

- **Discovery** (no token cost): model lists from the providers'
  APIs and harness options (version, accepted models and efforts),
  rate cards; a change (new model, withdrawal, price) produces an event
  in the thread of the team concerned. API credentials remain secrets
  of the host ([0014](0014-orchestrateurs-a-tours-et-placement.md)).
- **Catalogue**: execution state in the database (models seen, first and
  last appearance, price, context windows, efforts); the **evaluation
  reports** are published in the canon (OKF concepts, if needed
  `Attested Computation` for the metrics), hence readable and reviewed.
- **Evaluation**: suites of **reference tasks per class** (light
  fix, normal implementation, security review with known defects to
  find, writing, translation…), ideally drawn from real lots whose
  outcome is known; measures: success (green tests, defects found),
  cost, latency, tokens, per effort level. An evaluation campaign
  **consumes budget**: it is a **costly** action, subject to the gate
  (receipt or bounded standing approval) and to the plan safeguards (0019).
- **Recommendation**: `ameesh models recommend <task class>` proposes
  model and effort; the **routing policy** stays in the canon and only
  changes through a reviewed proposal (the orchestrator proposes, the responsible
  human validates).
- Requirement R21; lots L14 (catalogue and discovery) and L15 (evaluation and
  recommendation).
