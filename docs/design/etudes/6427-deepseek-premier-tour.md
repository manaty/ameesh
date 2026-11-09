---
type: Study
title: "Study — DeepSeek sessions at ≥300 k tokens from the first turn of a fresh session (measurement before fix)"
description: "manaty/ameesh#4955 (lot #6427): 24 h of the deepseek context corpus; the 300 k first turns are long turns (≈200–300 model steps, 25–30 min) growing ≈1.1–1.5 k tokens per step, not untruncated tool outputs; the in-turn context cap that would close them at a safe point and rotate exists but its 15 M default dates from the cumulative-reread measure (L60) and never trips since #2336; proposed fix: a per-harness context cap for deepseek, one implementation lot and one setting gesture."
status: draft
tags: [deepseek, contexte, rotation, plafond, mesure]
generated: { by: "mty-amh-impl-18/claude-opus-5-5", at: "2026-11-09T02:40:00+01:00" }
sources:
  - { resource: "../../../src/ameesh/runner.py", title: "turn_close_due, _turn_closed (L105, CTX-04a)" }
  - { resource: "../../../src/ameesh/adapters.py", title: "context_tokens vs reread_tokens (#2336)" }
  - { resource: "../../../src/ameesh/config.py", title: "context_max_tokens default (15 M), turn_max_seconds (1800 s)" }
  - { resource: "../../../src/ameesh/migrations/0041_plafond_de_contexte.sql", title: "L60: the cap defined on the cumulative reread" }
---

# Need

Night analysis of 09/11 (auditeur, angle 2, note on #6403): 48 deepseek turns
out of 319 at ≥300 k tokens of context, some on the first turn of a fresh
session (event 593263, deepseek2, 386 k; event 581750, mty-amh-impl-04,
357 k, lot #6186). #6403 (resume refused above the threshold) does not cover
this case. Working hypothesis: untruncated tool outputs. This study measures
before any fix.

# Method

`ameesh context corpus export --days 1 --harness deepseek`, window
2026-11-08T01:17Z..2026-11-09T01:17Z: 326 turns, 137 sessions, 0 skipped.
Per turn: `C_peak` (context of the largest model call, measured on the
stream), `R_turn` (cumulative reread, sum of the calls), `O` (output),
duration, `session_seq` and `R_session_base` (0 = really the first turn of
the session).

The stream gives no per-step series, so the number of steps is bounded:
at least `R_turn / C_peak`, and about `R_turn / ((C0 + C_peak) / 2)` if the
context grows linearly from a start `C0` ≈ 30 k (brief and prompt). The
growth per step follows: `(C_peak − C0) / steps`.

# Findings

## 1. Seven fresh sessions at ≥300 k on the first turn

| event | agent | C_peak | R_turn | O | duration | steps (min / ≈) | growth per step |
|---|---|---|---|---|---|---|---|
| 593263 | deepseek2 | 386 k | 61.4 M | 145 k | 25 min | 158 / 295 | ≈1.2 k |
| 581750 | mty-amh-impl-04 | 357 k | 57.7 M | 137 k | 30 min | 161 / 298 | ≈1.1 k |
| 587565 | mty-nxl-impl-04 | 346 k | 40.3 M | 165 k | 28 min | 116 / 214 | ≈1.5 k |
| 587218 | mty-amh-impl-03 | 326 k | 10.6 M | 167 k | 19 min | 32 / 59 | ≈5.0 k |
| 585963 | mty-nxl-rel-01 | 312 k | 43.5 M | 127 k | 26 min | 139 / 254 | ≈1.1 k |
| 592521 | mty-nxl-impl-02 | 310 k | 41.9 M | 120 k | 24 min | 135 / 246 | ≈1.1 k |
| 594478 | mty-nxl-impl-02 | 303 k | 39.2 M | 142 k | 30 min | 129 / 235 | ≈1.2 k |

An eighth candidate (585211, mty-nxl-impl-04, 319 k) is not a first turn:
its `R_session_base` is 24.6 M, the session started before the window.

## 2. The cause is the length of the turn, not the size of the outputs

Over the 137 first turns, the growth per step is the same for large and
small turns; only the number of steps differs:

| first turns | count | median duration | median steps ≈ | median growth per step |
|---|---|---|---|---|
| C_peak ≥ 200 k | 39 | 20 min | 166 | ≈1.3 k |
| C_peak < 100 k | 41 | 3 min | 22 | ≈1.5 k |

About 40–55 % of the growth is the model's own output (`O`); the rest,
≈700 tokens per step on average, is tool output and input. The hypothesis
of untruncated tool outputs is not supported for six of the seven cases;
only 587218 (≈5 k per step over ≈59 steps) points to larger outputs, and the
corpus cannot tell a few huge outputs from many medium ones without a
per-step series (see question 2). The turns that reach 300 k are long build
turns of 25–30 minutes, ended by `turn_max_seconds` (1800 s) or by the agent.

## 3. The guard that should close them exists but never trips

The runner already closes a turn at the next safe point when the context of
the last model call reaches `context_max_tokens` (`turn_close_due`, L105),
then forces a rotation with a resume summary before continuing
(`_turn_closed`, CTX-04a). `session_max_tokens` (150 k) only acts between
turns, which a single first turn bypasses.

The default `context_max_tokens` is 15 M (`config.py`, migration 0041). It
was set when L60 measured the **cumulative reread** of the turn; #2336
changed the measure to the **context of one call** without recalibrating
the value. No model call reaches 15 M: of the 326 turns of the window, none
is closed by the context cap, 8 are closed by the duration (1800 s, at
183–356 k of context, then continued in the same session), and
`turn_reread_budget` and `compact_tokens` are not set.

## 4. Scale of the effect

Over the window, 61 deepseek turns have `C_peak` ≥ 300 k: 8 on the first
turn, the others on turns 2–5 (the between-turn rotation and its minimum of
three turns, field of #6403). Linear estimate of what an in-turn cap would
have closed and saved on the 1 945 M tokens reread (the cost of the resume
summary is not counted):

| cap | turns closed | of which first turns | reread saved |
|---|---|---|---|
| 300 k | 61 | 8 | ≈172 M (9 %) |
| 250 k | 105 | 17 | ≈312 M (16 %) |
| 200 k | 154 | 39 | ≈478 M (25 %) |
| 150 k | 209 | 64 | ≈614 M (32 %) |

# Proposal

1. **Fix lot (sensitive: runner, rotation)** — a context cap by harness:
   `context_max_tokens` read per harness (descriptor or executor default per
   harness) before the global default, deepseek set at 250 k to start; the
   global 15 M stays for Claude and Codex until their own measurement. Tests:
   `turn_close_due` closes a deepseek turn at 250 k and leaves a Claude turn
   alone; the forced rotation follows. Estimate 2 h.
2. **Immediate gesture, without code (pilot)** — `ameesh set <agent>
   context_max_tokens=250k` on the deepseek agents, effective from the next
   turn, to measure the closures (`turn.context` kind `context_cap`) over
   24 h before choosing the default of lot 1. Reversible
   (`context_max_tokens=` to clear).
3. **Not proposed** — truncating tool outputs: the measurement does not
   support it.

The 250 k start value is a choice to confirm: it keeps 85 % of the turns
unchanged while closing every first turn above 300 k; 200 k saves more but
rotates one first turn in four mid-work.

# Open questions (pilot, mesh-design)

1. Start value of the deepseek cap: 250 k (recommended) or 200 k?
2. Should `dsh-json` record the per-step context series (largest step
   growth, `ΔC_max`) in `turn.context`, to settle the 587218 case and
   watch for large outputs later? Small additive lot, without decision.
