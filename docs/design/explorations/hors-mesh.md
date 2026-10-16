---
type: Exploration
status: exploration
title: "Talking to the outside: other meshes, off-mesh agents and humans"
---

# Talking to the outside: other meshes, off-mesh agents and humans

## Problem

A mesh is closed by construction. `agent-mail send` refuses a recipient that
is not in its registry, a stopped sender writes nothing, and a canon
synchronisation only creates the agents of the host's own canon. Above that,
[decision 0033](../decisions/0033-isolated-meshes-hosting-and-identity.md)
keeps two organisations' meshes isolated: two databases, two sets of runners,
no shared state and no bridge.

The need nevertheless shows up in operation. An integration agent of another
organisation (TPK), running against another database, could not write to this
mesh: its session belonged to the other base, the pilot it addressed was seen
as stopped there, and the bureau was not declared in it. It fell back to a
GitHub issue, which the bureau read and relayed into a lot by hand. A human
without a session on the host, or an agent of a partner mesh, meets the same
wall.

The question is not whether a mesh stays closed — it does — but what crosses
the boundary, under which identity, and with which trace.

## Intuitions

- Treat the boundary as an explicit gateway rather than an exception: a
  declared entry point (issue, signed mail, chat transport) deposits an
  *external* entry into the readable thread, without creating an agent row in
  the registry.
- Keep provenance visible: an external message carries its origin (federation,
  sender, channel, signature) and is never mistaken for a mesh identity; a
  signature or an allow-list decides what counts as proof, as for every other
  statement ([decision 0009](../decisions/0009-paroles-signees.md)).
- The GitHub-issue relay already works; it may deserve to be a documented
  primitive with a named owner and an acknowledgement, rather than an
  improvised fallback.
- A gateway can bridge addressing without sharing state: aliases, a directory
  of reachable meshes, or a relay operated by the bureau, each with its own
  refusal reasons. The canon already knows federations and members
  ([decision 0031](../decisions/0031-plusieurs-canons.md), [study on several
  canons](../etudes/plusieurs-canons.md)), and the messaging study compares
  transports ([Matrix, Zulip](../etudes/messagerie-annuaire.md)). Nexlink is
  the preferred channel of [decision 0003](../decisions/0003-ameesh-et-nexlink.md),
  including the distributed agent-mail idea of its AM1–AM15 spec.
- Inbound and outbound are asymmetric: what may leave the mesh (threads,
  private content, secrets) is a second question, settled apart from what may
  enter.

## Open questions

- Identity and addressing between distinct meshes or databases: a federated
  registry, aliases, or a gateway? Who owns a name that two meshes both claim?
- Which channel is canonical for an off-mesh agent or human: a GitHub issue,
  a signed mail, a chat transport, Nexlink? One entry point or several?
- Authentication and authorisation of inbound messages: signature, sender
  allow-list, quotas, refusal reasons readable from the outside?
- Traceability: how is an inbound message attached to a lot and to a decision,
  and who triages it?
- Outbound replies and confidentiality: what may leave, and how to answer
  without exposing private thread content?
- What remains manual on the bureau's side, and how is an inbound message
  acknowledged?
