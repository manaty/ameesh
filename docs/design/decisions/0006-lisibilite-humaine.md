---
type: Decision
title: "All exchanges are readable and auditable by humans"
description: "Agents talk to each other in human language, in channels the team can read; no hidden channel."
status: stable
tags: [audit, messagerie]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Context

"We are still lucky that AIs are LLMs and talk to each other in human language; let's take advantage of it." (the owner)

# Decision

All messages, **including agent to agent**, are **auditable by humans**: in natural language, in a thread the team can read, find and audit.

# Consequences

- The ameesh mailbox is only a queue or a cache; the reference is the readable thread.
- The body of a message is self-sufficient; structured metadata is added, never replaces it.
- One thread per lot.
- Readable does not mean authorised: authority is still proven by signature ([0009](0009-paroles-signees.md)).
