---
type: Exploration
status: exploration
title: "Provisioning a hosted ameesh-approve"
---

# Provisioning a hosted ameesh-approve

## Problem

The hosted modes of ameesh-approve require a consistent installation per team, with an isolated domain, state and identity. The gateway code, the local TLS and the read-only role already exist; their presence does not prove that the hosted offerings are deployed.

## Intuitions

Separating the existing primitives from provisioning on the provider's side could clarify the choice between a relay from a PC, a dedicated page and a dedicated server. A reversible preparation could preserve the RP ID and leave enrolment to the human.

## Open questions

- Which state should belong to the team rather than to its provider?
- How can an incomplete installation be noticed and resumed without merging two identities?
- Which evidence tells available code apart from a service that is actually running?

Existing context: [Contract of the three hosting modes](../ameesh-approve-hebergement-equipes.md), [Study of host provisioning](../etudes/l232-autonomous-host-provisioning.md). These references inform the question; they do not promote this card to a study or to decided work.
