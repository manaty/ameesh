---
type: Decision
title: "The canon is in OKF in git, federated according to OKF Federation"
description: "Teams, responsibilities, project structure are described in OKF; Nexlink and the tools are only views of it or proposal interfaces."
status: stable
tags: [okf, canon, gouvernance]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Context

The owner already maintains the OKF Federation specification (manaty/okf-federation) and federations that apply it.

# Decision

- The **teams with their responsibilities**, the **project structure** (modules, lots), the procedures and the decisions are described in **git repositories in OKF v0.2 format**, federated according to **OKF Federation**.
- **Nexlink pages are a rendered view of the OKF canon**; a change made in Nexlink becomes a **proposal** (a PR reviewed according to the federation's policy), never a second editable source. Same rule for any open source tool (Plane, Forgejo issues…).
- Agreement in principle for a federation root **manaty/home** (creation not yet scheduled).

# Consequences

- ameesh reads roles, canonical scopes and `review_policies` in `federation.yaml` and enforces them at execution time; it writes to the canon only by proposal.
- Execution state (in progress, blocked, waiting for a human) stays in ameesh and is projected onto a view (issue per lot, Nexlink page).
- An **ameesh profile** of OKF Federation is needed (types Agent, Team, WorkPackage, Pipeline; identity → key binding).
