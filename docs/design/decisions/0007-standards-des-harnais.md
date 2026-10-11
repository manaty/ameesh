---
type: Decision
title: "Integrations reuse the standards of agent tools"
description: "No ameesh-specific connector format: MCP, skills, harness plugins."
status: stable
tags: [mcp, skills, integration]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Context

Claude Code, Codex and DeepSeek Harness all speak MCP (checked locally: `codex mcp`, `codex plugin`, dsh embeds the MCP and ACP SDKs).

# Decision

Integrations with **business systems** and **communication** (Slack, Teams…) reuse **the standards of the agents' tools and plugins** (Codex, Claude, DeepSeek…); ameesh does not invent its own format.

# Consequences

- Outbound: standard MCP servers (official ones when they exist: Slack, Microsoft, Google, GitHub).
- Inbound: no portable standard; the ameesh runner receives the event and wakes the agent (Claude `-p --resume`, Codex app-server, ACP for the others).
- ameesh provides an MCP server "ameesh-fil" (thread), an MCP gate "ameesh-gate" (signed approval bound to the fingerprint of the arguments), and a generator from the OKF canon → skills, AGENTS.md, tool policy, plugin per harness. See [the study](../etudes/standards-des-harnais.md).
