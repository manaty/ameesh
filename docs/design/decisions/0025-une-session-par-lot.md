---
type: Decision
title: "A fresh session per lot"
description: "A build or review agent opens a fresh session for each lot and keeps it until the merge; orchestrators keep a long session, rotated with a summary."
status: stable
tags: [sessions, vitesse, qualite, cout]
decided_by: human:smichea
decision_date: 2026-10-05
attestation: "owner decision in his conversation with mesh-design; unsigned"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-05T04:20:00+02:00" }
---

# Context

An agent that had chained six lots on the same session (compacted context, more
than 60 million tokens re-read per turn) missed instructions and presented a
rebase as a fix, three freezes in a row, while it was already running at
maximum effort. See finding no. 5 of the
[findings on the models](../etudes/constats-modeles-2026-10.md).

# Decision

1. **Build and review agents: a fresh session per lot**,
   opened with a self-sufficient brief (state, scope, rules,
   known defects). The session is **kept until the lot is merged**,
   reviews and fixes included, then dropped.
2. **Reviewers**: a fresh session per reviewed lot, kept for the re-freezes of
   the same lot.
3. **Orchestrators**: long session, with rotation and a resume summary
   when it grows, and a written state (memory, journal) that survives the
   loss of the session.
4. **Monitoring**: the size of the context re-read per turn is tracked; a
   threshold triggers an alert, and a session that misses instructions is
   rotated without waiting.

# Consequences

- ameesh v1: in addition to rotation on size (L11), **rotation on lot
  change** (the turn's `work_item` changes → new session with the
  lot summary), configurable per agent: `par-lot` (default for build and
  review), `taille`, `jamais`; alert « session trop grosse » ("session too
  big"). Lot L26.
- In the meantime, the rule is applied by hand with the v0 loop (fresh session
  when a lot starts).

# L99 T5 — persisted delivery core (#4353)

Implementation of signed owner decision #164165 (29 October 2026,
approval #167): `merged` and `promoted` persist as `delivered`, with the
existing workflow stage and `legacy_state` preserving their distinction.
The dev module owns promotion and correction transitions. Historical
milestones, signed Process artifacts and public GitHub vocabulary keep
their contracts. Migration and rollback are qualified in a disposable
Postgres schema; the storage switch precedes executor restart.
