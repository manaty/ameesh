---
type: Study
title: "Study — real trial of Hermes Agent with DeepSeek, compared with dsh (L153)"
description: "Hermes Agent (Nous Research) and dsh, both on deepseek-flash, run the same two tasks in identical clones of this repository on ameesh-agents-1, two runs each. They come out even on fidelity (8/8 correct, near-identical diffs), on cost (about 0.03 USD a run) and on speed (dsh slightly faster). Hermes is heavier to install (about 1.3 GB, its own pinned runtimes) and has no event stream that ameesh already reads. Recommendation: stay on dsh; keep Hermes as an option through its ACP server if dsh is ever dropped."
status: draft
tags: [harnais, deepseek, dsh, hermes, cout, essai]
generated: { by: "ameesh-dev6/claude-opus-5-5", at: "2026-10-12T10:47:00Z" }
sources:
  - { resource: "https://github.com/NousResearch/hermes-agent", title: "Hermes Agent, commit bcec4b5 (2026-10-12), version 2026.9.24" }
  - { resource: "https://api-docs.deepseek.com/quick_start/agent_integrations/hermes", title: "DeepSeek: Hermes integration guide" }
  - { resource: "https://hermes-agent.nousresearch.com/docs/getting-started/quickstart", title: "Hermes quickstart" }
  - { resource: "../../../src/ameesh/cost.py", title: "Price list (DEFAULT_PRICES) and DeepSeek usage per step" }
  - { resource: "../../../src/ameesh/adapters.py", title: "Harness adapters (dsh headless --json)" }
---

# Context

Lot #150 (L153, issue manaty/ameesh#93). The study of 10 October recommended
trying Hermes without leaving dsh: a real trial on a simple task with a
DeepSeek key, then a comparison of cost, speed and fidelity with dsh. Owner
decision #9421 (signed, 2026-10-12): yes, with a 3 USD cap, on
ameesh-agents-1.

**Spent: about 0.26 USD** of DeepSeek, paid per token (8 runs and a smoke
test), well under the cap.

# Method

* **Same model on both sides:** `deepseek-flash`, the model dsh's `headless`
  profile uses by default. The DeepSeek API lists `deepseek-flash` and
  `deepseek-v4-pro`.
* **dsh 0.2.0-rc.2:** `dsh --profile headless --json -`, the way ameesh calls
  it.
* **Hermes 2026.9.24 (commit bcec4b5):** `hermes -z "<task>" --provider deepseek
  -m deepseek-flash --yolo --usage-file …`. `--yolo` lets Hermes run shell
  commands without asking, which matches what dsh headless does. Hermes kept
  its default toolsets, skills and memory.
* **Same workspace:** each run gets its own fresh clone of this repository
  (`git clone --shared`), at the same commit.
* **Same cost formula:** tokens from each harness (dsh: `step_end` events;
  Hermes: `--usage-file`, auxiliary calls included) × ameesh's price list
  (`deepseek-flash`: 0.27 / 0.07 / 1.10 USD per million input / cached input /
  output tokens).
* **Wall time:** measured around the process. Runs went in pairs at the same
  time (one dsh, one Hermes), so the times are only indicative.
* **Two tasks, two runs per harness for each:**
  1. **Synthetic, one file:** add `jours_ouvres(debut, fin)` to
     `src/ameesh/roadmap.py`, in constant time, with a French docstring and a
     new test file. Nothing else may change, no commit. **Oracle:** the
     agent's own test, plus a hidden test that compares the result with a
     day-by-day count on 3,000 random intervals.
  2. **Real:** the r2 verdict of PR #323 (lot #305), on `b70a67f`. Two P2s and
     one P3 in `progress_page.html`, with the matching source test to update.
     **Oracle:** a jsdom render of the page on a crafted snapshot that checks
     for the E1, `constructor`, `toString` and E3 groups, no "every task is
     finished" message, and no green E3 bar. Then `tests.test_progress_serve`
     and the list of files touched.

# Results

| run | task | time (s) | input / cached / output tokens | calls | USD | oracle |
|---|---|---|---|---|---|---|
| dsh 1 | 1 | 37 | 32,786 / 216,960 / 4,633 | 8 | 0.0291 | pass, hidden pass |
| dsh 2 | 1 | 69 | 25,925 / 212,224 / 8,140 | 9 | 0.0308 | pass, hidden pass |
| Hermes 1 | 1 | 59 | 35,087 / 262,400 / 5,210 | 10 | 0.0336 | pass, hidden pass |
| Hermes 2 | 1 | 61 | 34,818 / 229,376 / 4,074 | 9 | 0.0299 | pass, hidden pass |
| dsh 3 | 2 | 62 | 24,584 / 222,592 / 7,726 | 11 | 0.0307 | 6/6, tests OK |
| dsh 4 | 2 | 63 | 38,806 / 331,648 / 8,788 | 11 | 0.0434 | 6/6, tests OK |
| Hermes 3 | 2 | 79 | 26,228 / 268,800 / 9,491 | 12 | 0.0363 | 6/6, tests OK |
| Hermes 4 | 2 | 66 | 23,647 / 230,784 / 6,124 | 12 | 0.0293 | 6/6, tests OK |

* **Fidelity: tie.** All 8 runs are correct, touch only the allowed files,
  write French comments in the file's style, and leave no commit. On task 2,
  all four diffs use the same three fixes: `Object.create(null)` for
  `hasTask`, a guard `!(g.epic && g.epic.planned)` that keeps a planned epic,
  and "done" only when the group has tasks. dsh 4 also accepts an epic whose
  own actual dates say it is delivered.
* **Cost: tie.** Totals for 4 runs: dsh 0.134 USD, Hermes 0.129 USD.
  * **Hermes's fixed cost:** about 10,300 input tokens before the task even
    starts (smoke test "Réponds juste: ok": 0.0015 USD). Most of it is
    served from cache in a real run.
  * **Hermes's own estimate** in its usage file (0.0091 USD for Hermes 1)
    comes from a different price list. Compare with ameesh's formula, not
    with that figure.
* **Speed: dsh slightly ahead.** Mean 58 s for dsh against 66 s for Hermes,
  with a similar number of model calls. Hermes's start-up (Python 3.14,
  loading skills) accounts for part of the gap.

# Running Hermes in the mesh

* **Installation is heavy and messes with the host.**
  * **The official installer** (`curl … install.sh | bash`) writes to
    `~/.hermes` and adds `~/.local/bin` to `~/.bashrc`.
  * **pip fails:** the package refuses to build as a wheel ("Building wheels
    or sdists for hermes-agent is not supported").
  * **What worked:** a clone, then `uv sync --frozen --no-dev` into a venv,
    with `HERMES_HOME` pointing to a separate folder.
  * **Disk:** on first run, Hermes downloads its own pinned runtimes into
    `HERMES_HOME/tools`: Python 3.14, Node 26, npm and uv, about 690 MB.
    Counting the clone (360 MB), the venv (160 MB) and the uv cache (150 MB),
    that comes to about 1.3 GB for one instance. That matters on agents-1
    after the disk incident. dsh is a single npm package.
* **Headless mode exists but is not wired into ameesh.**
  * **What Hermes offers:**
    * `-z/--oneshot` for a single prompt;
    * `--usage-file` with tokens and calls;
    * `--resume <id>` to resume a session;
    * an ACP server (`hermes acp`, `hermes-acp`).
  * **What ameesh reads today:** the dsh `--json` event stream, for cost per
    turn, session id and the end of a turn (`cost.py`, `adapters.py`).
  * **To adopt Hermes:** either a new adapter (one-shot + usage file +
    resume), or going through the ACP bridge ameesh already has. The ACP
    route reuses existing code, but it would need its own trial.
* **Risk:** DeepSeek states that this integration is third-party and not
  guaranteed. Hermes moves fast (commit of the day, version "2026.9.24").

# Recommendation

* **Stay on dsh for the DeepSeek agents.** The trial shows no gain in cost or
  fidelity, and a small loss in speed. Hermes adds about 1.3 GB per host and
  needs integration work.
* **Keep Hermes as a fallback** if dsh were dropped or stopped being
  maintained, ideally through ACP. In that case, a follow-up lot would cover
  an ACP trial and a shared `HERMES_HOME` per host to install the runtimes
  only once.
* **Limits of this trial:**
  * two small tasks, two runs each;
  * one model (`deepseek-flash`), no `deepseek-v4-pro`;
  * no long session, no context compaction, no rotation;
  * runs in pairs at the same time, so the times are noisy.

  A long, multi-turn lot is where the harnesses' context handling would
  differ most.

# Reproducing the trial

The scripts were in the agent's temporary folder (cleared by the host's
cleanup). Their content:

* **`run.sh <dsh|her> <n>`:** `cd` into the run's clone, then either
  `dsh --profile headless --json - < task` or `HERMES_HOME=… hermes -z "$(cat
  task)" --provider deepseek -m deepseek-flash --yolo --usage-file …`, both
  under `timeout 1200`, recording the wall time.
* **Task 1 (French, given as is):** "ajoute à src/ameesh/roadmap.py une
  fonction publique `jours_ouvres(debut, fin)` … temps constant … docstring en
  français … tests/test_jours_ouvres.py (au moins 4 cas) … ne modifie aucun
  autre fichier … lance le test … ne fais pas de commit".
* **Task 2:** the text of the #323 r2 verdict (the three points above), on
  `b70a67f`, with permission to update only `tests/test_progress_serve.py`.
* **Hidden test for task 1:** 3,000 random intervals (seed 153), compared with
  `sum(d.weekday() < 5)` counted day by day.
