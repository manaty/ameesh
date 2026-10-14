---
type: Study
title: "Study — tests chosen by impact, failures reported in real time"
description: "Measurement of the CI of ameesh and Nexlink; measured test → code map (per-test coverage, files read, tables touched); impacted first; result stream to the mail of the lot's agent; lots L161a–L161i estimated and dated."
status: draft
tags: [ci, tests, couverture, selection, temps-reel, courrier, runners]
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-11T04:30:00+02:00" }
stale_after: 2027-01-11
sources:
  - { resource: "../../.github/workflows/tests.yml", title: "ameesh CI (L116: parallel parts)" }
  - { resource: "../../scripts/test-parts.py", title: "Splitting the suite into parts (L116)" }
  - { resource: "../EXPLOITATION.md", title: "Mail batched before wake-up (L125)" }
  - { resource: "decisions/0018-vitesse-des-agents.md", title: "Decision 0018 (turn interruption)" }
  - { resource: "etudes/boucle-auto-reparation.md", title: "Study: self-repair loop (A1, A8)" }
---

# Context

Owner's idea, 2026-10-11 at 03:25: "when we run the test suites on a
version, each suite could list the code files that concern it; when running
the tests, we would run first the tests most impacted by the version, and we
could report errors in real time to the agent, so that it fixes and pushes
again". Lot no. 171 of the tracker (L161), study only.

Starting point: ameesh's CI runs the whole suite on every PR, even when only
the version number changes; the agent only learns of a failure at the end of
the run; locally, the result depends on the machine. On the Nexlink side,
desktop tests on macOS and Windows only run on a tag: on the night of 10 to
11 October, two desktop tags failed at build.

# In brief

1. **Waste first, without a map.** ameesh: a documentation-only or
   version-only PR no longer runs the whole suite (18 % of the week's PRs,
   44 % of PR CI minutes since L116), and a main run is no longer cancelled
   by the next merge (L161a). Nexlink: no more Backend suite for a
   documentation-only push (80 % of its minutes on `develop`), and Windows
   and macOS desktop tests run as soon as a push touches `desktop/`, not only
   on a tag (9 of the week's 17 tags failed on these tests) (L161d).
2. **Report each failure as soon as it appears**: JSON lines stream, routed
   by ameesh to the mail of the lot's agent; a single urgent per pushed
   version, verdict as ordinary mail, inherited red never blamed on the
   author, withdrawal on the next push; agents no longer wait for CI in their
   turn (L161b, L161c).
3. **Measure the test → code link, never declare it**: per-test coverage,
   subprocesses included, data files read and tables touched, at function
   granularity; map regenerated on every merge to main (L161e).
4. **Impacted first, nothing else skipped on PRs** until leaks have been
   measured for two weeks; never anything skipped for code on the
   integration branch (L161f, L161i).
5. **Nexlink adopts stream, routing and selection** with its native tools
   (`--findRelatedTests`, `vitest related`, the migration's test first), plus
   a nightly map for e2e (L161g, L161h).

# Method

Read-only.

* ameesh: `gh run list` and `gh api …/jobs` on the 141 runs of the `tests`
  workflow (2026-10-04 22:11 UTC → 2026-10-11 01:20 UTC), of which 58 since
  L116 was merged (2026-10-10 18:07 UTC); timestamped logs of the seven
  failed runs; files of the 60 PRs.
* Agent waiting: duration of `gh pr checks --watch` and `gh run watch` calls
  in the transcripts of Claude sessions of this machine's main account,
  manaty projects only, last five days.
* Nexlink: workflows read on `develop`, 5,543 runs collected from 04 to
  11 October, logs of a sample of failures; two figures checked by sampling
  (desktop tags, documentation pushes).
* Map measurement mechanisms verified on a toy example (coverage.py 7.16.2,
  Python 3.14). No pilot on the real suite locally: host load of 13.4 for
  12 CPUs at the time of the study.

# What happens today

## ameesh's CI, measured

Since L116, a PR runs 4 psycopg parts and 1 psql part ("pilote" (driver)
modules); a push to main or `release/**`, 4 psycopg parts and 6 psql parts;
plus a macOS job (platform layer) and the `tests` aggregate.

| Since L116 | Median | p90 | Max | Runner-minutes per run |
|---|---|---|---|---|
| green PR, wall-clock time | 3.5 min | 6.2 min | 6.9 min | 13.3 |
| red PR, wall-clock time | 3.1 min | 4.8 min | 5.9 min | — |
| green main, wall-clock time | 5.9 min | 7.5 min | 8.7 min | 41.5 |
| before L116: green PR | 22.5 min | 25.4 min | 29.1 min | 31.1 |

* **Where a job's time goes**: in the test step (psycopg part 153 s median,
  PR psql part 86 s, main psql part 290 s). The rest is fixed and short:
  Postgres container 11 s, install 5 s. Fewer tests therefore means an
  almost proportionally shorter part.
* **Queue**: 40 jobs out of 424 waited more than 60 s for a runner (up to
  4 min); 9 runs out of 54 had at least one. This is the account's limit on
  concurrent jobs, reached when several pushes coincide (12 jobs per push to
  main, 7 per PR).
* **Cancellations**: 21 runs cancelled by concurrency, of which 7 on main
  (3 since L116). Those merges never had their own verdict.
* **Reference durations**: `tests/parts/durees.json`, refreshed by hand,
  only knows 76 of the 91 modules; the other 15 are weighted by estimate.

## Failures: when we could have known

Since L116: 5 red PR runs out of 33 (15 %), none on main, no rerun of the
same commit (no flakiness observed over the period).

| PR | Failing test | 1st failure | End of run | Gap | Impacted by |
|---|---|---|---|---|---|
| #53 | `test_cluster` (role contract) | +1.3 min | +5.9 min | 4.6 min | migration 0051, `deploy/sql/role-superviseur.sql` |
| #49 | `test_l61_allers_retours` | +1.1 min | +3.2 min | 2.2 min | `storage/postgres/projects.py` |
| #44 | `test_cluster` (role contract) | +1.0 min | +3.0 min | 2.0 min | migration 0049, `deploy/sql/role-superviseur.sql` |
| #43 | `test_l63_platform` (macOS) | +0.3 min | +3.1 min | 2.9 min | `platform/*.py` |
| #40 | `test_l117_account_spread` | +0.8 min | +3.1 min | 2.4 min | test module modified by the PR |

Before L116, the gap reached 8.2 and 9.6 min. The five recent failures
would all have been in the impacted set. Two come from a **dependency that
no import shows**: a new column (migration SQL file) must be granted or
excluded in `deploy/sql/role-superviseur.sql`, which `test_cluster` checks.
The link must therefore cover data files, not only Python code.

## What PRs change

Of the 60 PRs: 31 code, 12 code with migration, 7 documentation only,
4 version only (`pyproject.toml` alone: #48, #54, #99, #104), 2 CI or
scripts, 4 other (`deploy/`, examples). A code PR touches a median of
5 files in `src/` (p90: 20), often central files (`cli.py`, `main.py`,
`mesh_cli.py`, `runner.py`, `storage/interface.py`).

* The 11 documentation-only or version-only PRs (18 %) ran the whole suite.
  Since L116, that is 8 of the 19 PRs: 3.1 to 6.9 min each, 51 min in
  total, i.e. 44 % of PR CI minutes.
* A version bump on a `release/v1.6.x` branch ran the suite **three times**
  for one line: branch push (`release/**`, full suite, 5.8 to 8.7 min), PR
  (6.1 to 6.9 min, slowed down by the first), then main. Since 1.7.0, the
  branch is named `version/…`: two runs.
* "Documentation only" does not mean "no tests": `test_l33_chatbot` indexes
  `site/docs` and `docs/design` and checks a search; `test_canon` reads
  `examples/canon`. A rule based on path patterns would miss them; the map
  measures them.

## Agent waiting

* Per PR, cumulative CI (all its non-cancelled runs) is 6.3 min median
  since L116 (24.3 before); 1.6 runs per PR on average.
* Between the end of a red run and the corrective push: 1.8 to 16.1 min
  (median 6 min).
* Agents wait **within their turn**: `gh pr checks <n> --watch` (auditor
  prompt, step 5) or `gh run watch`. Over five days: 10 blocking waits on
  ameesh (median 3.6 min, p90 8.8, 43 min in total), 38 on Nexlink (median
  6.2 min, p90 9.2, 184 min in total), and 30 others launched in the
  background. This is a **lower bound**: neither Codex sessions, nor other
  accounts, nor the VM are counted, and the tool cuts a command off at
  10 min (several Nexlink waits stop at 9.7 min).
* Locally, the suite depends on the machine (test database, load,
  variables). `tests/support.py` already neutralises some thirty variables,
  but a machine under pressure cannot serve as a reference.

## Nexlink

From 04 to 11 October: 5,543 runs. The integration branch is `develop`;
`main`, the repository's default branch, is 1,179 commits behind. The real
flow is a push to `agent/**` then to `develop`, almost without PRs.

* **Where tests run**: on GitHub runners (`ubuntu-latest`), except the
  Windows and macOS matrices of Build Desktop and Host Rust. The self-hosted
  fleet so far only carries the latency test bench, a probe and a trial
  branch. The test bench measures it 1.6 times slower than GitHub for the
  backend suite (12.8 versus 8.1 min), with a queue of 2.1 min median
  (p90 5.8) versus 3 s.
* **No selection by change**: no `paths-filter`, no `--findRelatedTests`,
  no `--changedSince`; static `paths:` filters on some workflows, none on
  Backend suite.

| Workflow (event) | Runs | Median / p90 | Failures | 1st failure, before the end |
|---|---|---|---|---|
| Backend suite (push `develop`, `agent/**`) | 3,874 | 4.7 / 7.8 min | 32.7 % | ≤ 1.5 min: a single job, and jest already prints each failing file |
| Remote help (push) | 165 | 4.0 / 5.0 min | 17 % | 2.5 to 3.7 min: several jobs, signal per job |
| SQL rules (push) | 258 | 2.5 / 3.3 min | 11.6 % | ~0.1 min; ~1.5 min if the migration's test ran first |
| Build Desktop (tag) | 17 | 9.9 / 13.9 min | 88 % (15/17) | 8.6 to 10.2 min |

* **Backend suite on `develop`**: 81 % of the week's runs follow a push
  that only changes documentation (57 % of the last 30, by sampling). They
  carry 82 % of the failures, an inherited red that the push did not cause,
  and 80 % of the runner-minutes (≈ 7,900 out of ≈ 9,800). Backend suite
  consumes ≈ 20,000 runner-minutes per week in total.
* **What fails** (99 Backend suite failures): guardrails 55 %, jest unit
  23 %, e2e 16 % (including heap overflows).
* **Desktop, night of the 10th to the 11th.** Desktop tests only run on
  Windows and macOS in Build Desktop, on a tag; the desktop suite under
  review is Ubuntu only.
  * `desktop-v0.2.6` (19:43): red on Windows, 2 tests out of 776 (POSIX path
    expected, file mode).
  * `desktop-v0.2.7` (20:46): red on macOS (13 tests) and Windows
    (15 tests): host architecture, temporary directory as a symbolic link,
    `process.getuid` missing, `fsync` refused on a directory.
  * The agents worked around it with 3 manual launches of a Windows trial
    workflow and 7 pushes of trial workflows. `desktop-v0.2.8` is published
    at 01:39, six hours after the first red tag.
  * Over the week, 9 of the 17 tags failed on Windows (8) or macOS (1) tests
    never run before the tag; the other failures come from macOS signing (4)
    and publishing (4).
* **Tools**:
  * 18 independent npm packages (no workspaces, no turbo, no nx); the code
    of `shared/*` is copied by sync scripts;
  * jest 30 (backend, desktop, mobile), jest 29 (`shared/*`), vitest 5 and
    Playwright 1.59 (website); dependency-cruiser in the backend;
  * 79 of the backend's 87 e2e specs import `AppModule`: their import graph
    links them to almost everything;
  * SQL: 330 numbered migrations, 309 test files named by migration number;
    `scripts/sql-tests.sh` applies the whole chain (~44 s), runs the tests
    in numerical order and already accepts a list of numbers.

# 1. Test → code link, measured

## Why neither imports nor a hand-written list

* **Imports**: 65 of ameesh's 91 test modules launch the CLI or the runner
  as a subprocess (`python -m ameesh.cli`, `ameesh.runner`, `ameesh.main`),
  and `ameesh.main` reaches through imports 107 of the 125 files in `src/`.
  In static closure, a test module "depends" on a median of 107 files;
  68 modules out of 91 depend on more than 80 % of the code. Selection by
  imports would almost always choose everything.
* **Hand-written declaration**: it goes stale from the first PR, and it
  forgets precisely the invisible links (migration → `deploy/sql` →
  `test_cluster`, documentation → `test_l33_chatbot`). A declaration is only
  allowed as an "always" list for global objects (section 2), justified line
  by line, like `tests/parts/pilote-psql.txt`.

## Three measurements, one map

The unit of **selection** remains the test module (`tests/test_*.py`): L116
already splits by module, and a module keeps its `setUpClass` and its
throwaway schema. The unit of **measurement** is the test (per-test
context), so that it can be refined later.

1. **Python code executed, per test, subprocesses included** (coverage.py):
   * in the suite's process, one dynamic context per test
     (`dynamic_context = test_function`, or `Coverage.switch_context` set by
     the result class of `scripts/test-parts.py`);
   * in subprocesses, a start-up via `COVERAGE_PROCESS_START` and a `.pth`
     file (`coverage.process_startup()`), with a **static context read from
     the environment**: `context = ${AMEESH_TEST_CONTEXT-}`. The result
     class sets `AMEESH_TEST_CONTEXT = test.id()` at the start of each test.
     `child_env()` copies `os.environ`: the CLI and the runner launched by
     the test inherit it;
   * verified on the toy example: lines executed by the subprocess are
     attributed to the right test. The recent `[run] patch = subprocess`
     option does measure subprocesses, but passes them the parent's context
     (empty): it is not suitable;
   * dynamic contexts require the C tracer (coverage 7.16 refuses the
     `sysmon` core with them). Subprocesses, which only have a static
     context, can keep `sysmon` with their own configuration file. Overhead
     to be measured in L161e, off the critical path.
2. **Data files read, per test**:
   * a Python audit hook (`sys.addaudithook`), installed by the same
     start-up in the parent and in subprocesses, records the `open`,
     `os.listdir` and `os.scandir` events on repository files that are not
     Python;
   * it therefore sees migrations, `deploy/sql/*.sql`, harness descriptors,
     the package's `.md` prompts, `progress_page.html`, `pyproject.toml`,
     the documentation read by `test_l33_chatbot` and `examples/canon`;
   * verified on the toy example: an SQL file read by a subprocess is
     attributed to the right test;
   * reading a directory counts: a new file in `migrations/` touches the
     tests that list that directory.
3. **Tables touched, per class**:
   * each test class has its own schema (`t_<classe>_<hex>`). Before
     dropping it, `tearDownClass` reads `pg_stat_user_tables` for that
     schema (inserts, updates, sequential and index scans);
   * `pg_stat_force_next_flush()` flushes the statistics of the test's
     connection; subprocesses flush theirs when closing;
   * a migration is attached to the tables it creates or modifies
     (`create table`, `alter table`, `create index … on`,
     `create trigger … on`, `grant … on`).

**Code granularity: the function** (block), as pytest-testmon does for
pytest.

* Each Python file is split into blocks (functions, methods, rest of the
  module), with a fingerprint of their syntax tree without line numbers.
  A test module depends on the blocks of which it executed at least one
  line.
* A PR changes a block if its fingerprint differs between the map's commit
  and the PR's head.
* A new function is only called from a modified block: its tests are found
  through the caller.
* A module-level change (constant, dispatch table) touches all tests that
  imported that module: this is broad, but correct.
* At file granularity, `cli.py` or `runner.py` (4,661 lines) would choose
  almost the whole suite.

**SQL rule.** ameesh's migrations are **added**, never rewritten. A new
migration impacts:

* the tests that read the whole schema or `deploy/sql` (found by
  measurement 2: `test_cluster`, `test_migrations`);
* the tests that touch the tables it modifies (measurement 3).

A modified existing migration, or a non-additive migration (`drop`,
`rename`, type change), impacts all database tests.

## The map: content, storage, freshness

* **Production**: a `carte.yml` workflow, on every push to main (and
  manually), off the critical path, with "latest commit only" concurrency.
  It runs the psycopg suite under measurement in 4 parts, plus the pilote
  modules with psql (code specific to the psql driver only runs there), then
  merges the measurements.
* **Content**: `carte.json.gz`, schema `ameesh-carte/1`, on the order of 1
  to 2 MB.
  * main commit and tool versions;
  * per test module: duration per driver, failures of the last 14 days,
    flakiness;
  * per block: fingerprint and modules;
  * per data file and per directory: modules;
  * per table: modules;
  * the list of blocks that no test executes.
* **Storage**: GitHub Actions cache `carte-<sha>` (a PR reads the caches of
  its base branch; fallback key `carte-`), and run artefact (30 days) for
  ameesh and the agents. **Never in the repository**: with 25 merges to
  main in 7 h on the evening of 10 October, the map would change on every
  merge and create conflicts.
* **Durations**: the map replaces the hand-refreshed table.
  `test-parts.py` reads durations from the map; `durees.json` only remains
  as a fallback.
* **Freshness**:
  * a PR is compared to the **map's commit**, not to its base. What has
    changed on main since the map counts as changed: the selection widens,
    it never narrows;
  * map missing, older than 48 h or with no common ancestor: full suite.

# 2. Order and selection

## The order

The `plan` job classifies each module:

| Class | Content |
|---|---|
| P0 | modified or new test modules (absent from the map) |
| P1 | impacted: modified blocks, data files or directories read, tables touched by a migration; "always" list if its triggers change |
| P2 | failed in the last 7 days, on any branch |
| P3 | the rest |

`scripts/test-parts.py` distributes each class in turn across the parts
(heaviest first, into the least loaded part, as today): each part starts
with its impacted modules. Within a class, short modules run first, for an
earlier first signal. Today, a part runs in alphabetical order: an impacted
test may run last.

**"Always" list** (declared, justified): tests of objects global to the
Postgres cluster, which per-schema measurement does not see. These are the
roles created by `test_cluster`, the `LISTEN/NOTIFY` channels
(`test_events_attach`), outage and reconnection (`test_l72_panne_de_base`).
They are triggered as soon as a migration, `db.py` or `deploy/sql` changes.

## When to skip the rest

| Event | Change | What runs |
|---|---|---|
| push to main, `release/**`, tag, manual | everything | full suite, both drivers, impacted first; **never skipped, never cancelled** by the next push |
| PR | documentation only: files under `docs/`, `site/`, `examples/` or `*.md` outside the package | only the tests that read these files: declared until the map exists (`test_l33_chatbot` for `docs/design` and `site/docs`, `test_canon` for `examples/canon`), measured afterwards; under a minute |
| PR | version only: only the `version =` line of `pyproject.toml` | `test_version` and package build (`pip wheel . --no-deps`), under a minute |
| PR | test foundation or CI (`tests/support.py`, `tests/fakebin/`, `scripts/test-parts.py`, `.github/`, `pyproject.toml` dependencies), file unknown to the map, stale map | everything, impacted first |
| PR | code, general case | **phase 1**: impacted first, then the rest; **phase 2**: impacted only on PRs, the rest on main — **applied since 14/10/2026** (owner decision, canon §3, lot #1211: impacted plus the contract list, full suite on main, `release/**` and the package integration branch) |

**Nexlink.** There, the push to `agent/**` stands in for the PR, and
`develop` for main. Proposed exception, for the owner to decide: on the
integration branch, a commit that only changes documentation read by no
test takes over the verdict of the previous code commit, instead of
rerunning the suite (81 % of Backend suite runs on `develop`). On ameesh,
main keeps the full suite on every merge, as requested: 6 min, free
minutes.

**Provisional green light.** When P0 and P1 are green in all parts, ameesh
tells the agent (passive event). The agent may prepare the description or
request a review, never merge: merging requires `tests` green.

**Moving to phase 2** (L161i). We count **leaks**: a failure, in the rest of
a PR or on main, of a module that the map had not chosen for that change.
Phase 2 only comes if:

* leaks stay below 1 per 50 PRs for two weeks;
* and the selection is "safe": map less than 24 h old, no modified block
  unknown to the map, no foundation file.

The owner applied it ahead of that review on 14/10/2026 (canon §3, lot
#1211), after the CI queue passed 100 pending runs; the two conditions above
remain the safety net, applied per run by `scripts/test-plan.py`
(`lot_selection`, `phase2_safe`) rather than once for all: any doubt gives
the full suite — including a file **removed** outside documentation, since
the map knows only the readers it measured (`--no-renames`: a rename is a
removal plus an addition) — and the contract list
(`tests/parts/toujours.txt`) always runs on a lot branch. A **configuration**
file follows its measured readers, like any data file; only `pyproject.toml`
beyond its `version` line and `.github/**`, the configuration the tests
themselves run under, force the full suite (test infrastructure). The leak
count keeps its value: it measures what the selection misses on the runs
where the full suite does run.

A failure on main then goes to the agent of the merged lot (section 3).

**Free signal.** A modified block that **no test executes** is reported to
the agent ("lignes modifiées sans test", i.e. lines changed without a
test). In phase 2, it forces the full suite.

## Risks

| Risk | Countermeasure |
|---|---|
| flaky test: a pointless urgent wakes an agent and makes it hunt for a non-existent defect | immediate retry of the failing test, in the same process, before anything is sent; if it passes: "instable" (flaky), passive event, counted in the map; a quarantine goes through a lot, never silently |
| inherited red: the base was already red (82 % of Backend suite failures on `develop`) | the failure is not blamed on the author of the push (section 3) |
| hidden dependency (data, tables, variables, global objects) | measurements 2 and 3; "always" list; full suite on main; leaks counted |
| order dependency, revealed by the new order | the order stays deterministic (same change, same order, on main as on PRs); a nightly run of the full suite in reverse order detects these dependencies |
| wrong or stale map | comparison with the map's commit; full suite beyond 48 h; leaks measured |
| measurement changes behaviour (speed tests slower under coverage) | the map is never blocking: a failure under measurement is recorded, not carried over to PRs |
| shared data | one database per part, one schema per class, `TRUNCATE` on each test: already in place; roles are global, hence the "always" list |
| subprocess launched with an empty environment (it loses `COVERAGE_PROCESS_START`) | the map counts subprocesses without data and reports them |
| PR from a fork (public repository): read-only token | no stream, final verdict only |

# 3. Real-time reporting

## The result stream

`scripts/test-parts.py run --flow F.jsonl` writes one JSON line at the
end of each test (schema `ameesh-test-event/1`), plus one start line and one
end line per part:

```json
{"v": 1, "type": "test", "depot": "manaty/ameesh", "tete": "<pushed sha>",
 "fusion": "<tested sha>", "execution": 38097324740, "part": "psycopg 3/4",
 "module": "test_cluster", "test": "tests.test_cluster.SuperviseurRoleTest.test_…",
 "classe": "P1", "raison": "deploy/sql/role-superviseur.sql",
 "statut": "echec", "reessai": "echec", "duree_s": 0.8,
 "ts": "2026-10-11T00:08:17.776Z", "extrait": "AssertionError: … (40 lines at most)",
 "rejouer": "python -m unittest tests.test_cluster.SuperviseurRoleTest.test_…"}
```

JSON lines rather than incremental JUnit: each line is complete as soon as
it is written, whereas a JUnit XML is only valid once closed. JUnit, useful
to humans in GitHub, is produced at the end from the stream. For Nexlink, a
jest reporter and a vitest reporter write the same format.

## From the runner to ameesh

| Route | Latency | What it takes | For |
|---|---|---|---|
| **A. GitHub check run** updated during the tests (one annotation per failure), polled by ameesh | polled every 15 s | `permissions: checks: write` on the jobs; polling in `ameesh notify` on the designated host (`github.host`, as in L126), only for in-progress runs of open lots' branches; conditional requests (a 304 does not count against the quota) | ameesh and Nexlink, first: no endpoint, no secret |
| **B. Direct push** to ameesh, authenticated by GitHub's OIDC token | ~1 s | an HTTPS endpoint on the VM that verifies the token (GitHub issuer, audience `ameesh`, repository, branch); no stored secret; this is building block A1 of the [self-repair](boucle-auto-reparation.md) study | once that endpoint exists |
| **C. Self-hosted runner** that writes itself (`ameesh ci report`) | ~1 s | a Postgres role with insert-only rights on the results table; never the runner's role, since the PR's code runs on the CI runner | once the self-hosted fleet carries the suites |

A background step of the job (`scripts/ci-flow.py publish`) follows the
stream file and updates the "tests (flux)" check run; an `if: always()`
step closes it with the summary. In a multi-job workflow, a job ending in
failure is already a signal: polling forwards it without waiting for the
other jobs (2.5 to 10 min gained on Nexlink).

## From the result to the agent's mail

* **The lot**: by the branch (`work_items.branch`, L118; a PR on ameesh, an
  `agent/…` branch on Nexlink), otherwise by the `ameesh-work: <id>` line of
  the head commit, otherwise by the "Closes #n" of the lot's issue (L126).
* **The recipient**: the lot's assignee. If stopped, the rules for
  undelivered mail apply (the team's orchestrators).
* **The sender**: `ameesh` (`work.SYSTEM_SENDER`). Each message carries
  `work_item_id`: it is filed in the lot's thread.
* **What is sent**, in line with L125:

| Message | Kind | Effect on an idle agent |
|---|---|---|
| first confirmed failure (after retry) of the current head | `--urgent` | immediate wake-up; a turn in progress is not interrupted (reserved for authorised senders, [0018](../decisions/0018-vitesse-des-agents.md)): the hook delivers it at the next tool call |
| subsequent failures of the same head | ordinary | batched by the 90 s window: one more turn at most |
| final verdict (green, or red with the list) | ordinary | wake-up at the end of the window |
| impacted green (provisional green light) | passive event | read at the next turn, does not open a turn |
| flaky test (failure, then success on retry) | passive event | same |
| inherited red: the test was already failing on the last known run of the base (main for a PR, `develop` for an `agent/…` branch) | one line in the verdict, never an urgent to the author; reported once to the lot that turned the base red (first red commit, `ameesh-work:` line) | none for the author |
| failure on main (or `develop`) | `--urgent` to the assignee of the merged lot, event to the team's orchestrators, `ci_main_red` alert pushed by `ameesh notify` to the human in charge (it blocks deployments) | immediate wake-up |

* At most one urgent per pushed head: CI alone cannot trigger the
  `turn_churn` alert.
* **Content**: test, part, reason for the impact, error excerpt (40 lines
  at most), command to replay, head and link; "la suite continue, verdict à
  la fin ; une nouvelle poussée annule ce passage" (the suite continues,
  verdict at the end; a new push cancels this run).
* **Memory**: a results table (a migration, number reserved at the start of
  the lot). It keeps failures and flaky results per test, and summaries per
  module and per run, not every success (1,875 tests, dozens of runs per
  day). It is used to deduplicate sends, for the order (P2), flaky tests,
  inherited red, leaks and `ameesh ci status <lot>`.
* **Agent prompts** (ORCHESTRATEUR.md, AUDITEUR.md): after a push, no more
  `gh pr checks --watch` or `gh run watch` within the turn; the agent ends
  its turn or continues its work, ameesh wakes it up.

## New push: cancel, withdraw

* **GitHub**: the per-PR concurrency group already cancels the superseded
  run. On main, `cancel-in-progress` must no longer apply
  (`${{ github.event_name == 'pull_request' }}`): each merge keeps its
  verdict.
* **ameesh**: as soon as a new head appears for the lot's branch, polling
  drops the old run. CI messages **not yet delivered** for the old head are
  settled as "obsolète, remplacé par <sha>" (obsolete, replaced by <sha>)
  without being shown; a message already delivered stays, since the agent
  may have acted.
* **Self-hosted runners**: cancellation must kill the test containers
  (`if: always()` clean-up step, containers labelled per run); otherwise,
  the next push starts on a loaded machine.

# 4. What this requires

## From ameesh's GitHub CI

* A **`plan`** job (~20 s): enough history for the diff, the map restored
  from the cache, and `scripts/test-plan.py`, which outputs the nature of
  the change, the classes and the matrices (read via `fromJSON` from the
  job's outputs). `plan.json` is passed to the parts as an artefact.
* The **parts**: `needs: plan`, dynamic matrix, `--plan plan.json
  --flow flux.jsonl`, check run publication. A job with no module is
  skipped by `if:`, since an empty matrix is not allowed.
* The **`tests` aggregate**, the only required check, accepts `skipped`
  when the plan decided so. The check's name does not change, nor does
  branch protection. A `paths:` filter at the workflow level is not
  suitable: the required check would stay pending.
* `carte.yml` (section 1) and main's **concurrency**.
* The repository is public: minutes are free, the constraint is the number
  of concurrent jobs. Fewer jobs for PRs without code means less waiting
  for the others.

## From self-hosted runners

On Nexlink, the self-hosted fleet does not carry any suite yet, and the
test bench measures it as slower. Nothing in this study requires moving the
suites there. If they do go there, we need:

* the map in an **on-disk** cache on the runner: no 10 GB limit, instant
  restore, updated by the map job;
* warm services (Postgres, containers) **one per runner slot**, named by
  `RUNNER_NAME`, never shared between two concurrent jobs;
* slots reserved for **impacted** tests (`rapide` label): otherwise, the
  first signal waits behind another push's long suite. GitHub cannot
  prioritise jobs; labels do;
* route C for the stream, with an insert-only role;
* clean-up on cancellation.

## ameesh first, Nexlink next

* **Two Nexlink gains need neither map nor stream** and can start right
  away, in parallel (L161d):
  * stop running Backend suite for a documentation-only push;
  * run desktop tests on Windows and macOS as soon as a push touches
    `desktop/`, building on the suite-filters branch under review, with the
    Build Desktop matrix without signing or publishing.
* **ameesh first for the rest**: a single language, a single test
  framework, 91 modules; the CI and routing (mail, lots, `notify`) are in
  the same repository, and `test-parts.py` (L116) already exists. That is
  where the stream format, routing, messages, inherited red and leak
  measurement get settled.
* **Then Nexlink** adopts the stream and routing, with its own tools:
  * comparison base `develop`, never `main`: `github.event.before` on
    `develop`, `origin/develop` on `agent/**`; `fetch-depth: 0`;
  * `jest --findRelatedTests` and `vitest related --run` for unit tests: in
    JavaScript, imports are explicit and the module graph is enough at the
    first level, whereas ameesh's subprocesses ruled it out;
  * in SQL, the modified migration's test first (`sql-tests.sh <n>`), then
    the rest;
  * for e2e, which the graph links to everything (`AppModule`), a map from
    V8 coverage run at night, one run per spec (~445).

# 5. Lots, estimates and gains

## Lots

Durations: an agent's work up to the green PR, review included. Each lot
reserves its migration number at the start if it has one.

| Lot | Content | Depends on | Duration | Proposed date |
|---|---|---|---|---|
| **L161a** — ameesh: PRs without code, main concurrency | minimal `plan` job (documentation only with its declared readers, version only, foundation); aggregate that accepts `skipped`; `test_version` and package build for a version PR; `cancel-in-progress` limited to PRs | — | 2 h | 2026-10-12 |
| **L161b** — ameesh: stream and simple order | `--flow` (`ameesh-test-event/1`); order P0, P2 and "always" list; end of alphabetical order within a part; immediate retry; JUnit produced at the end | L161a | 3 h | 2026-10-12 |
| **L161c** — ameesh: real-time reporting | check run published during the tests; polling in `ameesh notify`; routing to the lot; messages per L125; inherited red; results table (migration); withdrawal on the next push; `ci_main_red`; `ameesh ci status`; prompts without `--watch` | L161b | 6 h | 2026-10-13 |
| **L161d** — Nexlink without a map | Backend suite not run for a documentation-only push (on `develop`: verdict of the previous code commit, if the owner accepts); Windows and macOS desktop tests on pushes that touch `desktop/`; SQL: modified migration's test first | — | 4 h | 2026-10-12 |
| **L161e** — ameesh: measured map | `carte.yml`: per-test coverage, subprocesses included; audit hook (files, directories); tables per class; block fingerprints; cache and artefact; durations kept up to date; overhead measurement | L161a | 6 h | 2026-10-14 |
| **L161f** — ameesh: selection by the map | P1 by blocks, files and tables; dynamic matrices; provisional green light; "lignes modifiées sans test"; leak count | L161b, L161e | 4 h | 2026-10-15 |
| **L161g** — Nexlink: stream and selection | jest and vitest reporters to `ameesh-test-event/1`; signal per job; routing by `agent/…` branch; inherited red on `develop`; `--findRelatedTests` and `vitest related --run` on unit tests | L161c, L161d | 6 h | 2026-10-16 |
| **L161h** — Nexlink: e2e map, self-hosted runners | nightly map from V8 coverage, one run per e2e spec; if the fleet carries the suites: route C, on-disk map, `rapide` slots, clean-up on cancellation | L161g | 6 h | 2026-10-20 |
| **L161i** — two-week review | leaks and gains measured against this document; owner's decision on phase 2 (ameesh, then Nexlink) | L161f | 1 h | 2026-10-30 |

Total: 38 h. Order:

1. L161a and L161d right away: independent, they are the largest measured
   gains per hour of work;
2. L161b and L161e in parallel;
3. L161c and L161f;
4. L161g, then L161h;
5. L161i two weeks after L161f.

L161a to L161d need no map.

## Expected gains

| Gain | ameesh (measurements since L116) | Nexlink (measurements of the week) |
|---|---|---|
| Changes without code | documentation-only or version-only PR: 3.1 to 6.9 min of CI → under 1 min (18 % of the week's PRs, 44 % of PR CI minutes since L116); a version bump: one suite run instead of two or three | no more Backend suite for a documentation-only push: ≈ 7,900 runner-minutes per week, and 82 % of its failures (inherited reds) that no longer wrongly wake anyone up |
| Desktop before the tag | — (the platform layer already runs on macOS on every PR) | Windows and macOS tests on pushes that touch `desktop/`: 9 of the 17 tag failures seen before the tag; the night of the 10th to the 11th would have cost one corrected push, not six hours and ten trial runs |
| First failure known to the agent | +3.0 to +5.9 min → +0.5 to +2 min: **2 to 5 min earlier** per red run (median 2.4 min from real time alone, 0.3 to 0.7 min more from the order) | per job: 2.5 to 3.7 min (Remote help), 8.6 to 10.2 min (Build Desktop); within the backend job: ≤ 1.5 min; SQL: ~1.5 min from the order |
| Waiting within the agent's turn | 43 min over five days recorded (lower bound) → 0: waiting leaves the turn | 184 min over five days recorded (lower bound) → 0 |
| Provisional green light | ~1 to 1.5 min instead of 3.5 for a code PR (to be confirmed by the map) | guardrails and unit tests green before e2e, which accounts for ~4 of the backend's ~5 min of tests |
| Phase 2, if adopted | verdict of a code PR ~1.5 to 2 min instead of 3.5 (to be confirmed: share of modules actually chosen) | unit tests: up to 1.9 min via `--findRelatedTests`; e2e: nothing without the nightly map |
| Integration branch | duration unchanged (nothing ever skipped), but each merge has its verdict, and a failure reaches the agent in ~1 to 2 min (impacted first, relative to main's previous commit) instead of ~6 | same for code |

Per ameesh PR, in phase 1: from 6.1 min of cumulative CI on average to ~3 to
4.5 min (−25 % to −50 % depending on the share of PRs without code). The
gain is concentrated on PRs without code and on red PRs. The rest of the
gain is the subject of the L161i review: the agent learns of a failure while
the suite is still running, and no longer blocks its turn.

# Out of scope

* Local selection (`ameesh test --impact` in a clean container): possible
  with the same map, but a machine under pressure must not serve as a
  reference; to be reconsidered after L161i.
* Mutation testing and learning-based prioritisation: moot as long as leaks
  are not measured.
