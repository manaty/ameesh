---
type: Study
title: "Study — agent tooling standards (MCP, skills, plugins, ACP)"
description: "State of the standards in October 2026, support per harness, inbound communication, existing connectors, ameesh-gate approval gate."
status: draft
tags: [mcp, acp, skills, plugins, gouvernance]
generated: { by: "mesh-design-etude/claude-opus-5-5", at: "2026-10-04T00:10:00+02:00" }
stale_after: 2027-01-04
sources:
  - { resource: "https://modelcontextprotocol.io/specification/2026-07-28/changelog", title: "MCP 2026-07-28" }
  - { resource: "https://aaif.io/projects/", title: "Agentic AI Foundation" }
  - { resource: "https://agentskills.io/specification", title: "Agent Skills" }
  - { resource: "https://agentclientprotocol.com/overview/agents", title: "ACP" }
  - { resource: "https://code.claude.com/docs/en/channels", title: "Claude Code channels" }
  - { resource: "https://github.com/deepseek-ai/deepseek-harness", title: "DeepSeek Harness" }
  - { resource: "https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps", title: "Slack: non-Marketplace limits" }
  - { resource: "https://github.com/agentgateway/agentgateway", title: "agentgateway" }
  - { resource: "https://www.ietf.org/ietf-ftp/internet-drafts/draft-farley-acta-signed-receipts-01.xml", title: "IETF draft on signed receipts" }
---

# Conclusion

All **outbound** goes through **MCP**, which every harness speaks. No
standard covers **inbound** in a portable way. ameesh therefore owns
**agent wake-up** and the **approval gate**, expressed with existing
standards, without a new connector format.

# Standards (October 2026)

- **MCP 2026-07-28** (AAIF / Linux Foundation): a heavily breaking revision —
  stateless, no more server → client requests (MRTR pattern), `subscriptions/listen`,
  **Tasks** extension, OAuth via CIMD. Both 2025-11-25 and 2026-07-28 will need to be served.
- **Agent Skills** (SKILL.md): open standard launched by Anthropic.
- **ACP**: Zed and JetBrains. **A2A** v1.0: AAIF. **AGENTS.md**: AAIF.
- **Plugins**: format specific to each harness, common content (skills + MCP
  config + hooks).

# Support per harness

| Harness | MCP | Skills | Blocking hooks | Headless driving |
|---|---|---|---|---|
| Claude Code | local/remote, OAuth | yes | PreToolUse (allow/deny/ask/**defer**), PermissionRequest | `-p --resume`, stream-json |
| Codex CLI | local/remote, OAuth+CIMD | yes | PreToolUse (MCP tools included) | `exec resume`; **app-server** (`turn/start`, `turn/steer`) |
| DeepSeek Harness | stdio + HTTP (OAuth undocumented) | yes | reuses Claude/Codex `hooks.json` | **native ACP**; SDK `--session-id` |
| Gemini CLI | yes | yes | BeforeTool | `-p`; native ACP |
| goose, OpenHands | yes | partial | not verified | native ACP |

# Describing a harness: existing standards

- **ACP registry** (RFD completed, registry stabilised in March 2026): each agent
  publishes an `agent.json` manifest (id, name, version, schema version,
  description, repository, licence, capabilities = ACP methods, authentication
  options, distribution) in a registry repository aggregated into a
  catalogue. This is the chosen basis for ameesh's harness descriptors
  (lot L16), extended with ameesh-specific keys for what ACP does not describe
  (driving without an interface and session resumption outside ACP, gauges and cost,
  hooks, permissions).
- **A2A Agent Card** (`/.well-known/agent.json`, A2A v1.0): describes a
  **remote** agent (identity, skills, endpoints) — useful if an ameesh agent
  member is exposed to other systems, not for a local harness.
- **MCP registry** (`server.json`): describes tool servers, not
  harnesses; useful for the connector catalogue.

# Inbound

Claude Code "channels" exist (preview, Claude-specific, allow
list, not with MCP 2026-07-28). The **ameesh runner** receives the event
(Slack Events/Socket Mode, Teams bot, Gmail push, IMAP IDLE, Matrix, Nexlink),
records it in the thread and **wakes the agent**: Claude via `-p --resume`, Codex
via the app-server, **the others via ACP**. The agent reads and replies through an
"**ameesh-fil**" MCP server.

# Existing connectors

| System | MCP server | Status |
|---|---|---|
| Slack | official `mcp.slack.com/mcp` | GA February 2026; internal or Marketplace apps |
| Teams, Outlook | Work IQ (Microsoft) | preview, M365 Copilot licence |
| Gmail, Google Chat | official Google | Developer Preview |
| GitHub | `github/github-mcp-server` (MIT) | stable |
| Forgejo | `goern/forgejo-mcp` | community |
| IMAP/SMTP | no official one | **to create** |

# Governance: ameesh-gate

Open source MCP gateways (agentgateway, IBM ContextForge, Docker MCP
Gateway, Microsoft MCP Gateway) filter and log but **none of them handles
a signed human approval bound to the action**. ameesh must be or embed
the gate: it alone holds the business credentials, so the rule "no
irreversible action without a signed receipt" holds whatever the harness.

```
digest = SHA-256(JCS{server, tool, arguments, target account, lot,
                     policy version, nonce, expiry})
```

The digest serves as the challenge for ameesh-approve; the gate
recomputes it on the actual `tools/call`, verifies the signature and single use,
executes, logs in the thread. While pending: error "en attente, réf. X" ("pending,
ref. X") (or `defer` with Claude).
The read/irreversible/costly classification lives in the **OKF canon**, not in
the MCP annotations (`destructiveHint` is only a hint). Defence in
depth: PreToolUse hooks, network sandbox.

# Pitfalls

- **Slack**: non-Marketplace apps limited to 1 request/min and 15 objects on
  `conversations.history`; ban on archiving other organisations' data
  and on training LLMs → **one internal app per organisation**;
  free plan: 90 days visible. The reference log is not Slack.
- **Teams**: protected APIs (7 to 10 days validation), billed notifications.
- **Google Chat**: 60 writes/min per space, shared between apps.
- **`claude -p`** loads the project's `.mcp.json` and hooks without confirmation:
  launch with `--bare` and an explicit configuration.
- dsh is in developer preview.

# What ameesh creates

1. driving adapters (Claude stream-json, Codex app-server, ACP);
2. inbound adapters and the ameesh-fil MCP server;
3. ameesh-gate and the digest and receipt specification;
4. OKF canon generator → SKILL.md, AGENTS.md, tool policy, plugin per harness;
5. missing connectors (IMAP/SMTP, carriers) as standard MCP servers.
