---
type: Study
title: "Study — open source messaging and directory"
description: "Matrix, Zulip, Mattermost, Rocket.Chat, Campfire, Stoat compared for agent accounts, per-lot threads, push and licences."
status: draft
tags: [messagerie, annuaire, matrix, zulip, open-source]
generated: { by: "mesh-design-etude/claude-opus-5-5", at: "2026-10-03T23:30:00+02:00" }
stale_after: 2027-04-03
sources:
  - { resource: "https://github.com/element-hq/synapse", title: "Synapse (AGPL-3.0)" }
  - { resource: "https://spec.matrix.org/latest/application-service-api/", title: "Matrix appservices" }
  - { resource: "https://element.io/server-suite/community", title: "Element Server Suite Community" }
  - { resource: "https://github.com/zulip/zulip/blob/main/LICENSE", title: "Zulip (Apache-2.0)" }
  - { resource: "https://zulip.readthedocs.io/en/latest/production/mobile-push-notifications.html", title: "Zulip push" }
  - { resource: "https://docs.mattermost.com/product-overview/editions-and-offerings.html", title: "Mattermost editions" }
  - { resource: "https://docs.rocket.chat/docs/our-plans", title: "Rocket.Chat plans" }
---

# Comparison (checked as of 2026-10-03)

| | Matrix (Synapse + Element) | Zulip | Mattermost | Rocket.Chat |
|---|---|---|---|---|
| Server licence | AGPL-3.0 or Element commercial; Tuwunel Apache-2.0 | Apache-2.0, no open core in the code | AGPL-3.0 (source), Team binaries MIT | MIT except proprietary `ee/` |
| Paid features relevant to us | multi-tenant, LDAP/SCIM, audit, console (ESS Pro) | push relay beyond 10 users | Team: 250 users, no SSO; retention, audit = Enterprise | Starter 50 users; no SSO or audit in Community |
| Agent accounts | **appservice**: `@agent-*` namespace, no rate limit, no token per agent | bot with API key | personal-token bots | personal tokens |
| Thread per lot | Space per project, room or `m.thread` per lot | **one topic per lot**, created by posting | `root_id` | `tmid`, discussions |
| Self-hosted mobile push | via Element gateway (content not transmitted); 100% free only Android/UnifiedPush | Zulip relay mandatory, otherwise recompiled app | TPNS without SLA, HPNS paid | gateway with quotas |
| Human device keys | yes (ed25519, cross-signing) — no arbitrary signing API | no | no | no |

Campfire (MIT) and Stoat (formerly Revolt, AGPL): not enough threads, bots or directory.

# Recommendation

1. **Matrix** (Synapse, Element X) with **one ameesh appservice** carrying all
   agent accounts. Clean licences; open standard; the only candidate with
   human device keys. Matrix serves as the **transport** for approval requests
   and receipts, not as proof (see [human authority](autorite-humaine.md)).
2. **Zulip** second: best "one topic per lot" model, native
   multi-organisation, fully Apache-2.0; weakness: mobile push.

To rule out: Mattermost and Rocket.Chat (free editions too restricted since
2025-2026).

# Pitfalls

- **Mobile push**: all store apps go through their vendor's relay.
  100% self-hosted push only on Android (UnifiedPush) or with one's own
  app — an advantage of Nexlink.
- **Matrix rate**: 0.2 message/s by default; appservice `rate_limited: false`.
- **AGPL**: use the APIs and appservices, do not modify Synapse/Element.
- Dendrite is in maintenance mode; the Rust landscape (Conduit, Tuwunel…) is fragmented.

# Not verified

Licence of Mostlymatter (Framasoft fork of Mattermost); threads and bots of Stoat
and Campfire; Rocket.Chat push quotas; Element pricing.
