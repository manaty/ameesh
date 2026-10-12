---
type: Decision
title: "ameesh-approve is reachable from the phone via a Nexlink page"
description: "For the project switchover, the approval service is exposed over HTTPS by a Nexlink page."
status: stable
tags: [ameesh-approve, nexlink, bascule, reseau]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T02:10:00+02:00" }
---

# Context

WebAuthn requires a secure context (HTTPS) and a stable domain (RP ID); the
phone cannot reach the PC's `localhost`. Three options: private network with
certificate, Nexlink page, dedicated domain ([v1 switchover](../bascule-v1.md), step 3).

# Decision

For the project switchover, **ameesh-approve is exposed via a Nexlink page**.

# Consequences

- The RP ID is the domain of the Nexlink page; a domain change forces
  re-enrolling the passkeys (or declaring related origins, WebAuthn L3).
- On the Nexlink side: the page must be able to serve (or relay over HTTPS) the approval
  service that runs under the dedicated user — to be examined with
  the Nexlink orchestrator (link with the remark "any existing
  infrastructure becomes a Nexlink page").
- Nothing is exposed before the service has passed its security review (L7);
  putting it online remains an act of the owner.

# Clarification (2026-10-05)

Owner decision, reported by mesh-design: the page is **ameesh's
page** (the ameesh-approve service, not a Nexlink content page),
**served by Nexlink on `ameesh.nexlink.ph`**. RP ID `ameesh.nexlink.ph`,
origin `https://ameesh.nexlink.ph`. The service stays on the PC's loopback
(127.0.0.1:8765, user `ameesh-approve`); the HTTPS relay and the means of
reaching the PC without an inbound connection are provided on the Nexlink side.
