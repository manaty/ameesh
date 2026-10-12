---
type: Decision
title: "Agents have a card in the OKF canon; their state stays in ameesh"
description: "Declaration of the agent (sponsor, role, capabilities, harness, budget, tools) in the canon; execution state in ameesh."
status: stable
tags: [agents, canon]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Context

Today an agent's existence rests on its launch scripts and on board messages; any agent can launch another.

# Decision

- Each agent has a **card** in the OKF canon (type `Agent`): responsible human, team, role and capabilities (`read`, `report-drift`, `propose`, never `approve`), harness and model, intended machine, budget, allowed tools, reviewers.
- The card changes only by **reviewed PR**.
- **The state** (session, lease, current machine, status, spending, mail) stays in ameesh.
- **Ephemeral agents**, limited to reading and proposing, may exist without a card; they still have a responsible human, **inherited from the agent that created them** and recorded by ameesh at their creation (R14).

# Consequences

- Nobody creates an agent or widens its rights without review.
- The registry of members' public keys can live in the canon, changed by reviewed PR.
