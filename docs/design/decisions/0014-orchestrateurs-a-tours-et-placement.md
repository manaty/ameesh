---
type: Decision
title: "Turn-based orchestrators; agent placement decided by the responsible humans"
description: "Orchestrators become turn-based agents with interactive attachment; the project's ameesh lead, or the infra people they delegate to, decides which agent runs where and with which credentials, within the limits set by the lead of each server."
status: stable
tags: [orchestrateur, placement, infrastructure, identifiants, supervision]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T01:10:00+02:00" }
---

# Context

The orchestrators (Nexlink, ameesh) are interactive sessions tied to the
PC: no lease, no reliable restart, their monitoring dies with the session.
Moving them to a server raises the question of credentials (subscription or
API key) and of who decides what runs where.

# Decision

- **Orchestrators become turn-based agents** at the switchover to
  ameesh: monitoring as a separate service that drops its events into their
  mailbox, wake-up by the runner, exchanges in the project thread, and
  **interactive attachment** on demand (`ameesh attach`) which takes the lease
  and hands control back to the runner on exit. First on the PC, then on a
  server.
- **Placement is a human decision**: it is **the project's ameesh
  lead** — who **may delegate to infrastructure leads** — who
  decides **which type of agent runs on which server**, and **with an API
  key or a plan**.
- **The lead of a server may impose their rules**, for example
  accepting only DeepSeek or Codex agents on their machine.
- **ameesh must make this management and its supervision easy.**

# Consequences

- New canon concepts: **Host** (machine or cluster, with its responsible
  human and its **policy**: allowed harnesses, providers and models, allowed
  credential modes — API key, plan —, quotas, data
  constraints) and **Placement** (agent → host, credential mode), modified by PR
  reviewed according to the federation roles (project lead, delegated
  infra leads, host lead).
- ameesh **refuses a placement that violates the host policy** and proposes
  the admissible placements; it never moves an agent on its own.
- **Secrets** (API keys, plan tokens) are never in the canon:
  the canon says which mode is used, ameesh helps install the secret on
  the host (keyring, secret manager) and checks that it is present.
- **Supervision**: per host and per project — placed agents, leases, health, costs
  per provider and per mode, plan consumption against their
  limits (R7), alerts to the responsible human concerned.
