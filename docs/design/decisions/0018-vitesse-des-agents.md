---
type: Decision
title: "Speed: review proportionate to risk, interruption, session rotation, measured lead times"
description: "ameesh encodes the speed rules validated for Nexlink: review policy per risk class in the canon, first-class turn interruption, session rotation with a summary, sessions robust to relocation, lead-time metrics per lot."
status: stable
tags: [vitesse, revue, interruption, sessions, metriques]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "owner decision passed on by the Nexlink orchestrator (message of 00:09), reported by mesh-design; unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T02:30:00+02:00" }
sources:
  - { resource: "nexlink:docs/board/messages/2026-10-04-0215-orchestrateur-velocity-rules.md", title: "Speed rules applied to Nexlink" }
---

# Context

A one-line fix cost 30 to 45 minutes. Observed causes: the same
review circuit for a style line as for a migration; messages read
only between two turns (a counter-order arrives too late); full
re-verification every time; mandatory relay orchestrator; sessions that
swell until saturation; resume broken after a folder rename.

# Decision

To be encoded in ameesh as requirements:

1. **Review policy per risk class**, declarative, in the OKF canon,
   by file scope: **light** (merge as soon as the targeted tests are
   green, review afterwards), **normal** (one review by another vendor, the
   details do not block), **sensitive** (freeze, review before merge, explicit
   agreement: SQL, security, native, contracts, production). When in doubt, the
   higher class.
2. **First-class turn interruption**: a priority message preempts
   the current turn (turn stopped, the same session resumed with that message
   at the top) instead of waiting for the end of the turn.
3. **Session rotation** driven by context size and turn
   latency, with a **resume summary** produced before the rotation.
4. **Sessions robust to the relocation** of a worktree (folder rename).
5. **Lead-time metrics per lot** (request → freeze → review → merge), exposed
   by `ameesh list` / `ameesh work`.

# Consequences

- Requirement R19; lot L10 (review policy in the canon and lead-time metrics:
  outside the runner) and lot L11 (interruption, rotation, relocation: in
  the runner, after L8).
- The risk class of a change is computed from the files touched
  and the canon policy; it also feeds the gate (a "light" merge
  can be a `reversible` action).

# L99 T5 — persisted delivery core (#4353)

Implementation of signed owner decision #164165 (29 October 2026,
approval #167): `merged` and `promoted` persist as `delivered`, with the
existing workflow stage and `legacy_state` preserving their distinction.
The dev module owns promotion and correction transitions. Historical
milestones, signed Process artifacts and public GitHub vocabulary keep
their contracts. Migration and rollback are qualified in a disposable
Postgres schema; the storage switch precedes executor restart.
