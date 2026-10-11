---
type: Study
title: "Findings on models and effort per lot type (night of 3 to 4 October 2026)"
description: "First findings from the Nexlink orchestrator, to be reused as reference tasks for evaluating models and harnesses."
status: draft
tags: [modeles, effort, evaluation, constats]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T03:15:00+02:00" }
sources:
  - { resource: "message from the Nexlink orchestrator, 2026-10-04 02:38", title: "Findings passed on to mesh-design" }
stale_after: 2026-11-04
---

# Findings (observations, not measured)

1. **Implementation, DeepSeek "flash" at maximum effort**: good quality,
   but long turns and sessions that saturate quickly (after 5 or 6 lots). Switched to
   high effort for routine implementation; maximum kept for native Rust.
2. **Sensitive review, Codex at medium effort** (SQL, security, native): very
   effective at finding real defects through independent probes (29 blocks
   in 24 h, almost all well-founded), but also blocks on details (fixed by
   proportionate review, [0018](../decisions/0018-vitesse-des-agents.md)).
   Low effort for light reviews (docs, scripts).
3. **Claude Opus**: SQL and security reviews, acceptance testing lead; **Sonnet at
   medium effort** for design and visual review (to be evaluated).
4. **Style or UI lots**: the biggest loss is the designer →
   implementer → reviewer handoff; a designer who makes the changes themselves is faster.
5. **Costly errors come mostly from long sessions** (compacted
   context, missed prompts), more than from the choice of model.

# Suggested reference tasks

Small UI tweaks with screenshot; SQL fixes with privilege probes;
native defects reproduced on a test bench; reviews with known counterexamples
(the night's review blocks, with their probe, are a ready source).

# Measurements and lessons (03:28)

- After the speed rules: maximum work → merge delay went from 100 min
  (window 01:25–02:25, 8 merges) to 21 min (02:25–03:25, 6 merges); small
  sample. What made the difference: proportionate review, rotation of saturated
  sessions with summary, automatic pulse (a 67-min turn spent waiting for
  a full suite), reviewers' fast mode.
- **Contaminated evaluation replay**: a replay with DeepSeek flash at low effort
  (6.5 min, ≈ $0.15, green tests, score 9/10) had access to the reference
  solution through the worktree (identical diff). Requirement: replay in an
  **isolated** repository (git archive + init, without history or remote), without access to the
  board or to the other worktrees.

# First isolated replay (03:46)

Reference lot: JS mobile fix of about 400 lines with tests, replayed
in an isolated repository.

| Configuration | Duration | Cost | Quality (reviewer) |
|---|---|---|---|
| DeepSeek flash, low effort | 9 min 40 | ≈ $0.32 (56k input, 3.9M cache, 32k output) | 8/10, OK with details |
| DeepSeek flash, high effort (reference) | ≈ 9 min | comparable | ≈ 9/10, OK with one detail |

Provisional conclusion: on this lot type, **low effort does not save
real time** (time dominated by tools — installation, tests, type checking — and
reading the code), costs about the same (cache dominates) and loses about one
quality point. **High effort by default for implementation**; low effort
only when reasoning dominates (small tasks without a heavy test
suite). The speed lever lies elsewhere: targeted tests, short sessions,
risk-based review. To be confirmed on other pairs (Rust, SQL). Reference
tool: `~/.local/bin/nexlink-rd`.

# Second isolated replay (05:13)

Light lot (translations and tests): DeepSeek flash at low effort, 14 min, green
tests, 850-line diff versus 543 for the reference (more verbose); the
reference at high effort had taken about 14 min. No gain in lead time:
**low effort is ruled out for implementation** (criterion
[0023](../decisions/0023-critere-d-adoption-des-reglages.md)).

# Code review by a cheap model (17:40)

Isolated replay of a real review (lot blocked by the Codex reviewer on 2 defects),
with DeepSeek flash at high effort: 6 min 17 s, ≈ 55k input tokens (1.9M
cached) and 34k output, a fraction of a dollar. **It finds the 2 reference
defects**, with the same analysis and executable probes; it adds 1 false
positive (due to a project policy it was unaware of) and 1 correct non-blocking
remark. Provisional conclusion: **equal recall, slightly lower precision** on
this sample. Next: pilot in **double review** on normal-class
lots before any transfer of responsibility; sensitive reviews remain
with the Codex and Claude reviewers. Implication for ameesh: the project policy
must be provided to the reviewer (canon), to avoid false positives of this kind.
