---
type: Decision
title: "Scope: coordinate mixed teams of humans + agents for any office work"
description: "ameesh is not only for software development: it coordinates humans and agents, on devices and servers, for any office trade."
status: stable
tags: [portee, metiers]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Context

v1 was designed for the Nexlink project: code agents in git worktrees, an orchestrator, a board.

# Decision

ameesh coordinates the work of **mixed teams of humans and AI agents**, running on **devices** (phones, PCs) and **servers**. Software development is **one case among others**. Target cases cited by the owner: an **after-sales support team** answering incoming e-mails; **transport coordinators** (pickups, receiving, packing, shipping); more generally **any trade where the human uses nothing but a computer**.

# Consequences

- Dev-specific notions are generalised: agents → members (humans or agents); worktree → workspace; intake→build→qa→merged→promoted cycle → one pipeline model among others; freeze/review/merge → deliverable/review/decision.
- New interfaces: inbound channel, outbound channel, business tools, signed proposed action, autonomy policy (see [the trades study](../etudes/metiers-sav-transport.md)).
