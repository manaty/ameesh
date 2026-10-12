---
type: Decision
title: "ameesh-approve: three hosting options to choose from, a host name and an RP ID specific to each team"
description: "PC with Nexlink relay (subscription), dedicated Nexlink page, or the team's server; WebAuthn RP ID exactly equal to the team's host, never a shared parent domain."
status: stable
tags: [ameesh-approve, webauthn, hebergement, equipes, nexlink]
decided_by: human:smichea
decision_date: 2026-10-05
attestation: "owner product decision (06:22) relayed by the Nexlink orchestrator, reported by mesh-design; unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-05T06:45:00+02:00" }
---

# Decision

1. When a team adopts ameesh, it chooses where **its** ameesh-approve runs:
   (1) on its PC, reachable through the Nexlink relay, for a small subscription;
   (2) on a dedicated Nexlink page; (3) on its own server, with the ameesh
   configuration simply giving the URL.
2. **One service per team**, never a shared page: each team has its
   host `H`, and its **WebAuthn RP ID is exactly `H`**. No shared parent RP ID
   (`ameesh.nexlink.ph`, `nexlink.ph`), no sharing by path, no
   ancestor/descendant relation between the RP IDs of two teams.
3. The name `ameesh.nexlink.ph` is **reserved**: it serves neither as RP ID nor
   as content. The owner's installation takes a team host like the
   others (proposed: `manaty.ameesh.nexlink.ph`, to be confirmed after Nexlink's
   same-site analysis); since no passkey has been enrolled, the change is
   free.
4. Changing hosting without re-enrolling passkeys requires keeping the same
   `H`, the same origin and the same registry; changing `H` forces a
   re-enrolment.

# Consequences

- Detailed contract: [ameesh-approve, three hosting options](../ameesh-approve-hebergement-equipes.md).
- On the ameesh side, lot L27: `approve_url` distinct from `public_url` and API
  explicitly openable, strict profile (RP ID equal to `H`, single origin,
  refusal of an RP ID equal to a zone or to a reserved parent), consistency of
  the policy between the service and the verifiers, DB rights specific to approve,
  switchover procedure with `H` constant.
- On the Nexlink side: namespace (after the same-site analysis), exclusive
  and non-reassignable allocation of names, gateway and tunnels per team,
  name kept when the subscription ends.
- Clarifies [0017](0017-approbation-via-page-nexlink.md): the page served by
  Nexlink is a team's instance on its own host.
