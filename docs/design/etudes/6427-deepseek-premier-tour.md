---
type: Study
title: "Study — DeepSeek sessions at ≥300 k tokens from the first turn of a fresh session (measurement before fix)"
description: "manaty/ameesh#4955 (lot #6427): 24 h of the deepseek context corpus; six of the seven fresh first turns above 300 k are long turns (≈210–300 model steps, 24–30 min) growing ≈1.1–1.5 k tokens per step, like small turns, not untruncated tool outputs; the in-turn context cap that would close them at a safe point and rotate exists but its 15 M default dates from the cumulative-reread measure (L60) and never trips since #2336; proposed fix: a per-harness context cap for deepseek, one implementation lot and one setting gesture."
status: draft
tags: [deepseek, contexte, rotation, plafond, mesure]
generated: { by: "mty-amh-impl-18/claude-opus-5-5", at: "2026-11-09T01:19:20Z" }
# first draft; the Decisions section (#202540) was added at 01:36:13Z (c68b32177)
updated: { by: "mty-amh-impl-05/deepseek-flash", at: "2026-11-10T12:53:33Z" }
sources:
  - { resource: "../../../src/ameesh/runner.py", title: "turn_close_due, _turn_closed (L105, CTX-04a)" }
  - { resource: "../../../src/ameesh/adapters.py", title: "context_tokens vs reread_tokens (#2336)" }
  - { resource: "../../../src/ameesh/config.py", title: "context_max_tokens default (15 M), turn_max_seconds (1800 s)" }
  - { resource: "../../../src/ameesh/migrations/0041_plafond_de_contexte.sql", title: "L60: the cap defined on the cumulative reread" }
  - { resource: "6427-data/tables.py", title: "Script that recomputes every table from the corpus" }
  - { resource: "#6677", title: "24 h measurement of the 250 k setting and per-harness fix lot" }
---

# Need

Night analysis of 09/11 (auditeur, angle 2, note on #6403): 48 deepseek turns
out of 319 at ≥300 k tokens of context, some on the first turn of a fresh
session (event 593263, deepseek2, 386 k; event 581750, mty-amh-impl-04,
357 k, lot #6186). #6403 (resume refused above the threshold) does not cover
this case. Working hypothesis: untruncated tool outputs. This study measures
before any fix. Its window (24 h up to 2026-11-09T01:17Z) is not the one of
the night analysis (48 of 319 turns at ≥300 k), so section 4 counts 61 of
326; the two named events are in both.

# Method

`ameesh context corpus export --harness deepseek --since
2026-11-08T01:17:00Z --until 2026-11-09T01:17:00Z`: 326 turns, 137 sessions,
0 skipped. Every table below is recomputed by
[`6427-data/tables.py`](6427-data/tables.py) from that corpus.
Per turn: `C_peak` (context of the largest model call, measured on the
stream), `R_turn` (cumulative reread, sum of the calls), `O` (output),
duration, `session_seq` and `R_session_base`. A **fresh first turn** is
`session_seq` = 1 and `R_session_base` = 0: 127 turns; the 10 others with
`session_seq` = 1 belong to sessions started before the window.

The stream gives no per-step series, so the number of steps is bounded:
at least `R_turn / C_peak`, and about `R_turn / ((C0 + C_peak) / 2)` if the
context grows linearly from a start `C0` ≈ 30 k (brief and prompt). The
growth per step follows: `(C_peak − C0) / steps`. Two `C_peak` of table 1
are `partial` (turn interrupted by the duration cap): lower bounds.

# Findings

## 1. Seven fresh sessions at ≥300 k on the first turn

| event | agent | C_peak | R_turn | O | duration | steps (min / ≈) | growth per step |
|---|---|---|---|---|---|---|---|
| 593263 | deepseek2 | 386 k | 61.4 M | 145 k | 25 min | 158 / 295 | ≈1.2 k |
| 581750 | mty-amh-impl-04 | ≥357 k (partial) | 57.7 M | 137 k | 30 min | 161 / 298 | ≈1.1 k |
| 587565 | mty-nxl-impl-04 | 346 k | 40.3 M | 165 k | 28 min | 116 / 214 | ≈1.5 k |
| 587218 | mty-amh-impl-03 | 326 k | 10.6 M | 167 k | 19 min | 32 / 59 | ≈5.0 k |
| 585963 | mty-nxl-rel-01 | 312 k | 43.5 M | 127 k | 26 min | 139 / 254 | ≈1.1 k |
| 592521 | mty-nxl-impl-02 | 310 k | 41.9 M | 120 k | 24 min | 135 / 246 | ≈1.1 k |
| 594478 | mty-nxl-impl-02 | ≥303 k (partial) | 39.2 M | 142 k | 30 min | 129 / 235 | ≈1.2 k |

An eighth turn with `session_seq` = 1 (585211, mty-nxl-impl-04, 319 k) is
not a fresh first turn: its `R_session_base` is 24.6 M, the session started
before the window.

## 2. The cause is the length of the turn, not the size of the outputs

Over the 127 fresh first turns, the growth per step is the same for large and
small turns; only the number of steps differs:

| first turns | count | median duration | median steps ≈ | median growth per step |
|---|---|---|---|---|
| C_peak ≥ 200 k | 38 | 20 min | 168 | ≈1.30 k |
| C_peak < 100 k | 37 | 3.9 min | 23.5 | ≈1.44 k |

About 40–55 % of the growth is the model's own output (`O`); the rest,
≈700 tokens per step on average, is tool output and input. The hypothesis
of untruncated tool outputs is not supported for six of the seven cases;
only 587218 (≈5 k per step over ≈59 steps) points to larger outputs, and the
corpus cannot tell a few huge outputs from many medium ones without a
per-step series (lot #6433). The six others are long build turns of 24–30
minutes, ended by `turn_max_seconds` (1800 s) or by the agent.

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
132–356 k of context; 7 continued in the same session, 1 was also rotated
on `session_size`), and
`turn_reread_budget` and `compact_tokens` are not set.

## 4. Scale of the effect

Over the window, 61 deepseek turns have `C_peak` ≥ 300 k: 7 fresh first
turns, 1 first turn of a session started before the window, 53 on turns
2–5 (the between-turn rotation and its minimum of three turns, field of
#6403).

Estimate of what an in-turn cap would have closed and saved on the 1 945 M
tokens reread, with the linear model of the method: a turn reaching the
cap has `k = (C_peak − cap) / g` steps left, which run in a fresh session
started at `C0` and each reread `cap − C0` tokens less, so
`saved = k × (cap − C0)`; the resume summary is not counted.

| cap | turns closed (of 326) | fresh first turns closed (of 127) | reread saved |
|---|---|---|---|
| 300 k | 61 (19 %) | 7 (5.5 %) | ≈172 M (9 %) |
| 250 k | 105 (32 %) | 16 (12.6 %) | ≈312 M (16 %) |
| 200 k | 154 (47 %) | 38 (29.9 %) | ≈478 M (25 %) |
| 150 k | 209 (64 %) | 61 (48.0 %) | ≈614 M (32 %) |

# Proposal

1. **Fix lot (sensitive: runner, rotation)** — a context cap by harness:
   `context_max_tokens` read per harness (descriptor or executor default per
   harness) before the global default, deepseek set at 250 k to start; the
   global 15 M stays for Claude and Codex until their own measurement. Tests:
   `turn_close_due` closes a deepseek turn at 250 k and leaves a Claude turn
   alone; the forced rotation follows. Estimate 2 h.
2. **Immediate gesture, without code (pilot)** — `ameesh set <agent>
   context_max_tokens=250k` on the ameesh deepseek agents (mty-amh-*,
   bounded by decision 2 below), effective from the next
   turn, to measure the closures (`turn.context` kind `context_cap`) over
   24 h before choosing the default of lot 1. Reversible
   (`context_max_tokens=` to clear).
3. **Not proposed** — truncating tool outputs: the measurement does not
   support it.

The 250 k start value is a compromise: it leaves 87 % of the fresh first
turns (and 68 % of all turns) unchanged while closing every first turn
above 300 k; 200 k saves more but rotates almost one fresh first turn in
three mid-work.

# Decisions (mesh-design, 2026-11-09, message #202540)

1. Start value of the deepseek cap: 250 k.
2. Setting gesture granted for 24 h on the ameesh DeepSeek agents only
   (mty-amh-*), with a receipt of the prior values, a per-agent stop on two
   forced rotations in a row on the same lot without progress, and a return
   to the prior values at the end unless the fix lot is merged.
3. Per-step context series in `turn.context`: lot #6433.
4. The fix lot (per-harness cap) waits for the 24 h measurement.

# 24 h measurement (2026-11-09T01:30Z → 2026-11-10T01:30Z)

The gesture of decision 2 set `context_max_tokens=250k` at 01:30Z on the ten
ameesh DeepSeek agents (`mty-amh-*`; receipt: no prior setting, default 15 M).
Corpus: `ameesh context corpus export --harness deepseek --since
2026-11-09T01:30:00Z --until 2026-11-10T01:30:00Z` — 545 turns, 225 sessions,
0 skipped; the 24 h before (2026-11-08T01:30Z → 2026-11-09T01:30Z) is the
baseline — 329 turns, 136 sessions.

**Closures `context_cap` and the forced rotations that follow** (scoped
`mty-amh-*` agents; `decision.close.kind = context_cap`):

| agent | closures | lots |
|---|---|---|
| mty-amh-impl-01 | 2 | #6525, #6534 |
| mty-amh-impl-03 | 2 | #6473, #6002 |
| mty-amh-impl-04 | 2 | #6422, #6520 |
| mty-amh-impl-06 | 2 | #6475, #6487 |
| mty-amh-impl-07 | 1 | #6437 |
| mty-amh-impl-08 | 2 | #6563, #6566 |
| **total** | **11** | eleven distinct lots |

Every closure (11/11) is followed by exactly one `context_cap` rotation,
published on the next turn: no closure without its rotation, no rotation
without a closure. Context at closure: 250,176–256,045 tokens (the first call
at or above the cap). The per-agent stop of decision 2 (“two forced rotations
in a row on the same lot without progress”) never triggered: the eleven
closures sit on eleven distinct lots. Outside the gesture, the Claude agents'
own 400 k cap also closed 9 turns (auditeur 4, mesh-design 4, orchestrateur
1); that setting is unchanged.

**Fresh first turns above 300 k** (`session_seq = 1`, `R_session_base = 0`):

| fresh first turns | before | after |
|---|---|---|
| all deepseek | 7 / 127 (5.5 %) | 13 / 214 (6.1 %) |
| scoped `mty-amh-*` | 2 / 59 (3.4 %) | 0 / 70 (0.0 %) |
| non-scoped deepseek | 5 / 68 (7.4 %) | 13 / 144 (9.0 %) |

The scoped share falls to zero; the two before cases are impl-04 (356,668,
event 581750) and impl-03 (326,345, event 587218). The global share does not
fall because the gesture does not cover the other DeepSeek agents (nexlink,
`deepseek6/7`, `ameesh-dev3`): they produce the 13 after cases. The first
event at 250 k is at 01:42Z (impl-03/05), the last at 15 M at 01:52Z
(impl-04); an agent idle at the gesture only shows a 250 k turn when it next
runs (docs-01, impl-02, impl-08: 07:19–07:21Z), so no closure can precede it.

**Settings returned?** No. At 2026-11-10T12:44Z the ten scoped agents still
carry `context_max_tokens=250000` (prior value: none, default 15 M), instead
of the return of decision 2. It is reported on lot #6677 and was not modified
here; the per-harness default of the fix lot carries the same 250 k once
merged. **No tort found**: the start value stays 250 k.
