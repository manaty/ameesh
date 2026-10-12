---
type: Decision
title: "The resident agent: you talk to an agent, you do not take it"
description: "An executed agent keeps, when its harness allows it, a harness process open under a lease; humans talk to it through ameesh chat then the Nexlink page; a human message is served at the end of the turn, !urgent at the safe point; attach becomes the exception; fallback to one turn per event."
status: accepted
tags: [agent-resident, conversation, chat, attach, bail, autorite]
decided_by: human:smichea
decision_date: 2026-10-10
attestation: "owner acceptance of the 7 recommendations of the study, in his conversation with mesh-design; unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-11T22:30:00+02:00" }
sources:
  - { resource: "../etudes/agent-resident.md", title: "Study: the resident agent" }
---

# Context

To talk to an agent, the human takes it: `ameesh attach` sets an exclusive
lease and the runner steps aside. If the human puts the session in the
background or forgets it, nobody wakes it any more, and mail, prompts and
alerts pile up. This is the "human session" form of the blockage of
2026-10-07 ([0030](0030-pas-de-travail-sans-reveil-possible.md)). The owner
wants the opposite: an agent available at all times to listen and answer, to
which the human comes to talk.

# Decision

1. **The agent stays driven by ameesh.** An `exécuté` agent (driven by a runner, 0030)
   whose harness declares a resident protocol (Claude Code stream-json, Codex
   app-server, ACP) keeps a harness process open under a lease, renewed
   continuously. Otherwise, it stays at one turn per event. Opening or closing
   a conversation changes nothing for the agent.
2. **You talk to an agent.** `ameesh chat <agent>` (terminal) first, then the
   Nexlink page of the real-time view (0024); Slack later. An agent's
   conversation is single and shared by its participants, and its exchanges
   go to the thread (R12).
3. **Order of service.** A human message received during a turn is served at
   the end of the turn. `!urgent`, from an authorised human, is served at the
   safe point (end of the tool call in progress, or native interruption).
   Mail, events and alerts come after human messages.
4. **Authority.** ameesh writes the header of each input. An agent message
   never has the owner's authority. A verified human message (local identity
   linked to the canon, then OIDC, 0033) informs the agent but does not
   replace an approval (0012).
5. **Availability.** A resident can sleep (inactivity, host pressure, account
   change): lease kept, wake-up at the first input. It is restarted
   automatically on its session; after repeated failures, it is stopped with
   an alert to the manager (0030).
6. **Sessions.** Per-message measurement, native compaction, rotation with
   summary when quiet (0025, L60). Cost is counted per message served.
7. **`attach` becomes the exception** (native interface: login, settings,
   debugging). It puts the resident to sleep, has a maximum duration, alerts
   when inputs are waiting, and hands control back to the resident. `adopt`
   and `resume` stay. The adoption of a live external session is reduced to
   refusing the duplicate.

# Consequences

- L75: resident protocols (`ameesh.resident` descriptor block, three protocol drivers).
- L76: resident runner (`ResidentWorker`, continuous heartbeat, sleep and wake-up); starts after L72 is merged.
- L77: input queue, priorities, token envelope and local identity.
- L78: conversation log and `ameesh_chat` NOTIFY, written to the thread.
- L79: `ameesh chat <agent>` terminal client, attached to lot #122.
- L80: cost per message on the ledger.
- L81: rotation of the resident session (continuous measurement, native compaction).
- L82: `attach` redefined (maximum duration, alert); duplicate refusal replaces database lot no. 12.
- L83: conversation in the Nexlink page (OIDC identity, write by the server).
- A migration to reserve after 0041. Overlaps: L72, L61, L60, L71, L31b, 0024, D1 and L55.
