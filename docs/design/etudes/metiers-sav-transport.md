---
type: Study
title: "Study — business domains: email customer service and transport coordination"
description: "What ameesh needs to put a customer service team and transport coordinators to work: open source stacks, non-code agents, governance of irreversible actions."
status: draft
tags: [metiers, sav, transport, gouvernance]
generated: { by: "mesh-design-etude/claude-opus-5-5", at: "2026-10-03T23:30:00+02:00" }
stale_after: 2027-04-03
sources:
  - { resource: "https://github.com/zammad/zammad", title: "Zammad (AGPL-3.0)" }
  - { resource: "https://github.com/OCA/delivery-carrier", title: "OCA delivery-carrier" }
  - { resource: "https://github.com/karrioapi/karrio", title: "Karrio" }
  - { resource: "https://www.odoo.com/documentation/20.0/developer/reference/external_rpc_api.html", title: "Odoo JSON-2 API" }
  - { resource: "https://arxiv.org/abs/2506.12469", title: "Levels of autonomy (Feng et al., 2025)" }
  - { resource: "https://goteleport.com/blog/owasp-top-10-agentic-applications", title: "OWASP agentic Top 10" }
---

# Customer service answering inbound email

- **IMAP (IDLE) + SMTP** as the foundation (Gmail and Microsoft 365 do not speak JMAP);
  JMAP optional for a self-hosted server (Stalwart).
- Low volume: plug email directly into ameesh (one thread = one lot, lease
  on the conversation). As soon as SLAs, queues, macros, customer history are needed:
  **Zammad** (AGPL-3.0, full REST API, webhooks, community MCP server;
  its AI classifies and prioritises without sending to the customer). FreeScout: paid API;
  Chatwoot: proprietary `enterprise/`, designed for chat; UVdesk discontinued.

# Transport coordination

- **Odoo Community** (LGPL-3.0) + **OCA `delivery-carrier`** (AGPL-3.0): Odoo's
  native carrier connectors are reserved for Enterprise. Target
  the **JSON-2** API (XML/JSON-RPC deprecated since 19.0).
- **Karrio** (LGPL-3.0, except `ee/`) as carrier gateway: DHL, UPS,
  FedEx, Chronopost in production; GLS and DPD missing.
- **Fleetbase** (AGPL-3.0) only for in-house pickup rounds.
- **An organisation that already has its own business systems** (transport
  management, quotes…) first plugs in its APIs as business tools; Odoo and
  Karrio are the open source reference for the others.

# Non-code agents

The "headless turn on a persistent session" model remains valid. What changes:

- the workspace is no longer a git worktree but a **business object**
  (`ticket:1234`, `picking:WH/OUT/0042`): the lease applies to it;
- review applies to a **proposed action** (draft, label,
  refund), not to a commit;
- the trigger is **external** (email, webhook) with an SLA.

Harnesses: Claude Agent SDK, OpenAI Agents SDK (serialisable pause
`needs_approval`), Codex, goose, OpenHands. "Computer use" as a last resort
(portals without an API).

# Governance

| Signed human approval | Without approval |
|---|---|
| any sending to the customer; refund, credit note, goodwill gesture; booking or cancelling a pickup, buying a label; change of address or declared value; amount above a threshold; deletion or merge of customer data | reading, classifying, summarising, draft, quote, tracking |

Progressive autonomy per action type (five levels, Feng et al. 2025), level
increase based on measured metrics. AI Act art. 50 (in force since
2026-08-02): AI disclosure in outgoing messages. Moffatt v. Air Canada:
the company is liable for its agent.

# What ameesh must add

- interfaces: **inbound channel**, **outbound channel** (only via an approved
  action), **business tools** (MCP, classified read / reversible /
  irreversible / costly), **signed proposed action**, **autonomy policy**;
- requirements: leases on external objects; **idempotence** of irreversible
  actions; legally enforceable audit log; **untrusted inbound content**
  (prompt injection); AI disclosure; GDPR; dedicated bot identities.
