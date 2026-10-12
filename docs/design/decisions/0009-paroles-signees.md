---
type: Decision
title: "Every statement is signed; no interim procedure before ameesh v1"
description: "Messages from humans and agents must be signed to count as proof; our current sessions remain unsigned."
status: stable
tags: [securite, signature, autorite]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Context

mesh-design passed on to the orchestrator "owner decisions" that nothing proved; the orchestrator believed them.

# Decision

- What authors say, **humans and agents alike**, must be **signed** to count as proof: a **provenance** signature on every message, an explicit **authority** signature for a decision.
- **No interim rule**: as long as ameesh v1 is not in service, the current sessions remain unsigned ("it's not secure, it doesn't matter").

# Consequences

- Prerequisite: one key per agent that the others cannot read (a Unix user or container per agent, or a signature made by the runner on the agent's behalf).
- A decision reported by an agent is proof only if the human signed it (ameesh-approve, passkey on the phone); it is then recorded as a `Decision` with its signature.
- In Slack, Teams or Google Chat, the platform identity remains a clue, never a proof.
