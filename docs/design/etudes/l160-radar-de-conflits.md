---
type: Study
title: "Study — conflict radar between lots, resolutions prepared before the merge, code written to be additive"
description: "Measurement of the merge rework of 10/10 (ameesh and Nexlink), what git already allows (merge-tree, rerere, merge queue, syntactic driver, fragments), comparison with finer-grained tracking (block, symbol, CRDT, Pijul, jj), design chosen for ameesh and estimated lots L160a–L160h."
status: draft
tags: [fusion, conflits, git, rerere, merge-tree, file-de-fusion, conventions, radar]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-11T04:15:00+02:00" }
stale_after: 2027-01-11
sources:
  - { resource: "../../ORCHESTRATEUR.md", title: "Orchestrator prompt (lot branch, merge without PR, reserved migrations)" }
  - { resource: "https://github.com/manaty/ameesh/pulls?q=is%3Apr+is%3Amerged", title: "Merged PRs (L118, L125), which replace the design log" }
  - { resource: "../decisions/0018-vitesse-des-agents.md", title: "Decision 0018 (agent speed, proportionate review)" }
  - { resource: "https://git-scm.com/docs/git-merge-tree", title: "git merge-tree --write-tree" }
  - { resource: "https://git-scm.com/docs/git-rerere", title: "git rerere" }
  - { resource: "https://git-scm.com/docs/rerere", title: "rerere, conflict normalisation (technical documentation)" }
  - { resource: "https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue", title: "GitHub, merge queue" }
  - { resource: "https://mergiraf.org/", title: "Mergiraf, syntactic merge driver" }
  - { resource: "https://docs.jj-vcs.dev/latest/conflicts/", title: "Jujutsu, first-class conflicts" }
  - { resource: "https://pijul.org/manual/theory.html", title: "Pijul, theory of patches" }
---

# Context

**Owner's idea (11/10, 03:25)**, quoted as is: "when an agent works on its lot
branch, one of the merge problems is that we don't know in advance the order of
the merges and the potential conflicts to resolve. Adding files is never a
problem (this may perhaps influence the way we code); we could imagine that
agents modifying a file notify the agents listening on that file, so that the
other agents can prepare several versions of their change with or without the
other agent's version. This probably assumes a system somewhat different from
git, but at merge time we would already have all the conflict resolutions
ready." A system finer-grained than the file.

**Observation from the night of 10/10**: almost all ameesh lot merges needed
rework (`docs/design/log.md` at the top of every PR,
`deploy/sql/role-superviseur.sql`, `ITEM_COLUMNS` in
`storage/postgres/work.py`, `storage/postgres/projects.py`, migration numbers
0048 to 0053 colliding). On the Nexlink side, the orchestrator serialises merges
by hand, through "merge go-ahead" ("feu de fusion") messages.

# Measurements

## Method

All read-only: mirror clones of both repositories in a throwaway folder, the
mesh database in a `read_only` transaction, PRs and CI runs through `gh`.

* **Replayed merges**: each two-parent merge commit is redone with
  `git merge-tree --write-tree`; an exit code of 1 says a conflict had to be
  resolved, and the list of files follows. With `merge.conflictStyle=diff3`,
  each conflicting block is classified: **addition** (the base is empty: both
  sides inserted at the same place) or **modification**.
* **Worktree reflogs** (one per agent): "commit (merge)" is a merge stopped on
  a conflict and then committed by hand; "rebase (continue)" is a rebase step
  stopped and then resumed. Rebases are invisible in the history: only the
  reflogs show them. The VM's agents are not included (other clones).
* **Mail**: messages of the Nexlink team, classified by regular expression
  (feu de fusion, develop frais, refetch, sans forcer, rebase, conflit,
  merge-tree…); delays between the orchestrator's go-ahead and arrival on
  develop, matched through the lot label in the merge commit title.

## ameesh, night of 10/10 (20:00 → 03:20)

* **22 merged PRs**, 4 of them releases. Of the **18 lot, fix and prompt PRs**,
  **11 needed at least one resolution by hand** and 2 others catch-ups with
  `main` without conflict (L63: 2, L119: 3). Only the PRs opened and merged
  within 15 min escaped it. Delay between opening and merge: median 20 min.
* **19 resolutions by hand** in 7 h 20: 10 stopped merges of `main` (3 of them
  on the track B integration branch) and 9 resumed rebase steps, in 11 of the
  13 worktrees that merged or rebased.
* Conflicting files in these 10 merges: `docs/design/log.md` **8 times out of
  10**; `docs/EXPLOITATION.md`, `exploitation.py`, `notify.py` 4 times;
  `storage/interface.py`, `backend.py` 2 times; once
  `role-superviseur.sql`, `storage/postgres/work.py` (`ITEM_COLUMNS`),
  `storage/postgres/projects.py`, `cli.py`, `mesh_cli.py`, the decisions
  index, the CLI reference, the list of "psql driver" modules, the version
  number (`pyproject.toml`, `__init__.py` and its test) and six others. 77
  conflicting blocks, of which **35 additions at the same place (45%)**; 4
  merges out of 10 had only additions.
* Over the whole public history (since 05/10): 27 conflicting merges out of 51,
  181 blocks, **92 additions (51%)**; the 12 blocks of `log.md` are all additions.
* **Migrations**: on 10/10, number 0048 was taken by four lots and there were
  12 renumberings, 9 of them for the three track B migrations (three times
  each, up to 0051, 0052, 0053). Git sees **no conflict** there (different file
  names): only `migrations.discover` ("version de migration en double",
  duplicate migration version) or a test reveals it, after the fact.
* Textual resolution is fast (a stopped rebase lasts 30 s at the median): the
  cost lies elsewhere, in the agent turn it opens, the rerun CI (3.6 min at the
  median, 36 commits tested for 22 PRs) and the wait of the following lots.
* `rerere` is **already active** on the workstation (global configuration,
  `rerere.autoUpdate`): 153 recorded conflicts for ameesh (142 resolved), 111
  of them on 10 and 11/10, and 343 for Nexlink. It only helps after the fact,
  and only on the workstation.

## Nexlink, day of 10/10 (12:00 → 04:00)

* **develop** received 275 first-parent commits (classified by title):
  **75 lot merges**, **29 catch-up merges** (develop moved during another's
  merge: "merge develop before the push"), 129 notices and review reports, 18
  migration number reservation commits, 24 others.
* Textual conflicts are rare: 20 merges committed by hand and 4 resumed rebase
  steps in the reflogs of 74 worktrees; 10 conflicting merges on replay, 5 of
  them on the migration reservations registry. Over 15 days: 6% of merges into
  develop and 10% of merges of develop into a branch, **53% of blocks are
  additions** at the same place; hot files: the three JSON translation files,
  the reservations registry, the knowledge log, the roadmap, the project
  tracking cards. The agents already do **trial merges** with `merge-tree`
  before merging, by hand.
* **The cost is serialisation**:
  * between the orchestrator's first go-ahead and arrival on develop:
    **median 39 min, 3rd quartile 76 min, maximum 3 h 08** (33 matched
    merges); between the last review GO and the merge: 8 min at the median,
    roughly the duration of a merge when the lot is at the head;
  * queue kept by hand: "go-ahead given, position 7 in the queue";
  * from 19:00 to 23:00, **382 messages out of 1,345 (28%)** are about merging;
    the orchestrator writes 76 of 332; "feu de fusion" appears literally in 35
    messages of the day.

## What a radar would have seen

Trial merges between active branches, measured on the present state:

| | Branches | Pairs | Pairs touching a same file | Conflicting pairs | Conflicting with the target | Full pass |
|---|---|---|---|---|---|---|
| Nexlink (pushed since 10/10 12:00) | 62 | 1,891 | 43 (2.3%) | 31 | 12 | 9 s |
| ameesh (21 open PRs, mostly the v2 stack) | 21 | 210 | 112 (53%) | 72 | 16 | 3 s |

A trial merge costs 0.02 to 0.07 s per pair. At Nexlink, overlapping pairs are
rare: a radar would warn seldom, and rightly. At ameesh, almost everything
overlaps because of a handful of central files (`EXPLOITATION.md` 69 pairs,
`main.py` 57): these files must be changed **first**, otherwise the radar
shouts constantly.

# 1. What git already allows

## Trial merges without a worktree

`git merge-tree --write-tree A B` (git ≥ 2.38, `--merge-base` ≥ 2.40) computes
the merge without an index or a folder: it returns the merged tree, the list of
conflicting files and exits with 1 if there are any. It only writes objects;
with `GIT_OBJECT_DIRECTORY` pointed at a throwaway folder and the repository in
`GIT_ALTERNATE_OBJECT_DIRECTORIES`, it writes **nothing** in the agents'
repository (verified). The granularity is already **the block**: two lots that
modify different functions of a same file are not in conflict. Limit: it only
sees textual conflicts; two 0048 migrations, or a function deleted on one side
and called on the other, go through.

**Verdict**: the building block of the radar.

## Recorded and replayed resolutions (`rerere`)

`rerere` records each resolved conflict (conflicting sides and result) and
replays the resolution when the same conflict comes back. The conflict is
identified by its two sides **sorted in a fixed order**: the same resolution
works whether A merges before B or the reverse. This is exactly "with or
without the other's version" for a pair of lots. The `rr-cache` is in the
repository's common folder, hence shared by all the worktrees of a host.

Limits: it only learns **after** a resolution; it does not move from one host
to another; if a third lot changed the same lines, the conflict is new; it can
silently replay a resolution that has become wrong (`autoUpdate`), hence the
gates on the merged tree; its entries expire (`gc.rerereResolved`, 60 days).

Sharing between agents, two ways:

* copy the `rr-cache` files into a dedicated ref: git's internal format,
  fragile;
* **share resolution commits**: the agent merges the other's branch into a
  throwaway branch made from its own, resolves, commits; this commit R(A, B) is
  pushed under a dedicated ref. Any host relearns the resolution with
  `rerere-train.sh` (shipped with git, `contrib/`), which replays merges to fill
  the cache. A commit is native, pushable to GitHub, reviewable and testable.

**Verdict**: chosen, through resolution commits.

## Merge queue with stacked testing

A queue lines up the ready lots and tests each one merged **onto the target
plus the lots ahead of it** ("merge train"); a lot that breaks leaves the queue
without blocking the others. GitHub offers it (`merge_group`) for public
organisation repositories, hence ameesh, and for private repositories only on
GitHub Enterprise Cloud: not Nexlink, on the free plan, which merges locally
anyway. Equivalents: GitLab trains, Zuul, Mergify. A queue resolves no
conflict: it removes the lot.

**Verdict**: principle chosen; the queue is kept by ameesh, the same for a
PR-based team and for a team that merges locally.

## Syntactic merge driver

Mergiraf merges by syntax tree (tree-sitter): two additions in a set whose
order does not matter (imports, class members, keys of a JSON object, according
to what each language declares) are no longer in conflict. It understands
Python, TypeScript, JSON, YAML and TOML, but neither SQL nor Markdown, and
nothing inside a string (`ITEM_COLUMNS` is an SQL string). It installs as a git
driver:

```
git config merge.mergiraf.driver "mergiraf merge --git %O %A %B -s %S -x %X -y %Y -p %P -l %L"
echo '*.py merge=mergiraf' >> .gitattributes
```

What it cannot merge stays in conflict, as before. GitHub runs no external
driver: it only applies to merges done locally, which is the case for rework.
The `union` driver built into git keeps both sides of a file made of
independent lines, at the risk of interleaving them.
`merge.conflictStyle=zdiff3` shows the base in the markers and helps the agent
that resolves.

**Verdict**: driver to be measured on the 103 replayed conflicting merges
before adopting it; never for SQL or Markdown. **Measured (L160g, 12/10)**:
not adopted, for any language. Replayed on 115 conflicting merges, mergiraf
fully resolves 16, but 13 of its results are wrong (4 do not compile, one
leaves conflict markers in a file reported as merged); see
[the L160g pilot](l160g-mergiraf-pilot.md).

## Fragments rather than central files

This is the owner's intuition: an added file never conflicts. A log made of
one fragment per lot, assembled on read (like towncrier, changesets or reno),
an index generated from the cards' header, a per-folder registry filled by
discovery, a number reserved in advance remove the conflict class instead of
resolving it. They cover the hottest files of both repositories: `log.md`, the
decisions index, the translation files, the reservations registry, the
knowledge log. Two caveats: two lots that create the **same path** are in
conflict, and two equal numbers in two different files go through without a
conflict; a probe is needed. And reorganising a central file conflicts with
all open branches: to be done with an empty queue, announced.

**Verdict**: the safest gain; to be done first.

## What ameesh already knows

* A lot carries its branch, its target and the last commit seen ahead
  (`branch`, `branch_target`, `branch_head`, L118, migration 0050).
* Each host's runner sweeps every 5 min the lot branches of its agents
  (`plan_git.sync_branches`, `AMEESH_BRANCH_SWEEP_INTERVAL`) in the assignee's
  working folder, and detects the merge by content (ancestor, patch-id, squash,
  `ameesh-work:` line).
* Mail attaches to a lot (`--lot`); a passive message (`ack`, `cc`,
  `broadcast`) wakes no one and is read at the next turn (L125); an `event`
  wakes at most once per 120 s window.
* Missing: the files touched per lot, the overlapping pairs, the merge order,
  the resolutions.

# 2. Tracking finer than git?

| Approach | Real gain | Cost | GitHub and PRs |
|---|---|---|---|
| Listening **per file** | Warns early | Noise: 53% of ameesh pairs overlap | Indifferent |
| **Per block** (trial merges) | The real conflict, nothing more | Low: 9 s per pass | Compatible |
| **Per symbol** (syntactic driver) | Additions in a same list or class are no longer conflicts | One binary per host, limited to certain languages | Compatible locally |
| **CRDT** | No markers: two concurrent insertions are ordered automatically | The code can be wrong without anything saying so; an operation log to keep; made for live co-editing, not for branches lasting several hours | Incompatible |
| **Pijul** | Commuting patches: a resolution applies in any order, the owner's idea natively | Another system, another hosting, tools, harnesses and CI to redo | Incompatible |
| **Jujutsu (jj)** | First-class conflicts: a rebase never stops, the resolution follows the descendants; useful for lot stacks | Stored in git and pushed to GitHub, but a conflicted commit must not be pushed; the harnesses speak git; warns no one | Compatible |

The useful granularity is **the block**, and git already provides it through
trial merges. The symbol only adds automatic merging of additions, which a
syntactic driver or a convention also provide. No system removes **semantic**
conflicts: only the gates on the merged tree, in merge order, catch them.
Changing tools would cost compatibility with GitHub, PRs and the harnesses, for
a gain that git and ameesh can provide. jj remains to be tried later for long
lot stacks (v2 stack).

# 3. Chosen design

Four pieces, in the order in which they pay off: additive conventions, radar,
prepared resolutions, queue and merge by ameesh.

## 3.1 Radar

* **Where**: the runner of the project's designated host (setting
  `radar.projects`, like `github.projects`), right after the branch sweep. It
  works in a **private clone** of the project (`--shared`, repository objects
  as alternates: neither copy nor writing in the agents' repository), with its
  own refs, its own `rr-cache` and throwaway worktrees. It reads the local
  branches of the host's common repository, which are those of all the
  worktrees, and `origin` for the agents of other hosts. Proposed rule
  (question 3): these agents push their lot branch at the end of every turn
  that created a commit; the radar only sees what is committed.
* **When**: at each sweep pass (5 min), and only for what changed: a pair is
  only recomputed if one of its two tips or the target moved (cache per triplet
  of commits). A full pass costs 9 s on the largest project.
* **What**, for open lots that have a branch:
  1. the files touched since the merge base with the target;
  2. the pairs touching a same file, the only ones submitted to `merge-tree`
     (one conflict per pair), plus each lot against the target;
  3. **probes** for what git does not see, declared per project: duplicate
     migration numbers (ameesh `src/ameesh/migrations/NNNN_`, Nexlink its
     migrations folder), same path created twice, number reserved by another
     lot;
  4. the result in the database (pair, commits, files, blocks, state `clean`,
     `overlap`, `conflict`, `prepared`, `stale`), migration reserved at the
     start of the lot.
* **Listening**: implicit, each lot listens to the files of its own diff; no
  subscription to maintain. Optionally, `ameesh work watch <lot> <chemin>`
  declares a file one is about to touch, to be warned before having committed
  (central file, migration number).
* **To whom and how**, without needless wake-ups (L125):
  * to the two assignees of the pair, by mail **linked to the lot** (`--lot`),
    passive (new `radar` key among the passive keys): read at the next turn,
    never a turn opened for it; also in the lot's thread and `ameesh work
    show`;
  * only on **state change** (new conflict, stale resolution, other lot
    merged), once per pair and per pair of commits;
  * a wake-up (ordinary mail, grouped over 90 s) only when the conflict
    **blocks**: the other lot is merged or at the head of the queue and the
    resolution is not ready; never `--urgent`;
  * the orchestrator receives no per-pair mail: one line in `ameesh projects`
    ("conflits prévus : n, préparés : p", conflicts expected: n, prepared: p)
    and the `merge_blocked` alert when the head of the queue has been waiting
    for a resolution for 30 min;
  * never a human, except through this alert.

## 3.2 Resolution prepared by the agent concerned

* **Who**: for each conflicting pair, a single **resolver**, the one of the two
  lots that will merge second in the planned order (3.3); failing that, the one
  whose diff is smallest in the conflicting files. The radar says so in its
  mail.
* **How**: `ameesh work resolve <lot> --with <autre lot>` opens a throwaway
  worktree from the lot's branch, merges the other's branch into it and lets
  the agent resolve; on commit, the resolution commit (trailer
  `ameesh-resolution: <lot> <autre>`) is pushed under a dedicated ref
  (`refs/ameesh/resolutions/<lot>/<autre>`, or a branch
  `ameesh/resolve/<lot>-<autre>` if the hosting service refuses refs outside
  `refs/heads`) and recorded in the database. The lot's branch does not change:
  it remains the version **without** the other, the resolution commit is the
  version **with** it.
* **Validity**: the radar replays the pair's merge in its clone, cache primed
  by `rerere-train.sh` on the resolution commits; if the whole conflict is
  replayed, the pair is `prepared`. A new push that changes the conflicting
  blocks makes it `stale`, and the resolver is warned (passive). A rebase does
  not make it stale as long as the conflicting blocks stay the same: `rerere`
  recognises the content, not the commits.
* **When**: as work goes on, at the next turn, not at merge time: the agent
  still knows its code, and no one is waiting.

## 3.3 Merge order chosen by ameesh

* One queue per target (repository and branch), in the database. A lot enters
  it when it is ready: freeze, review GO, green CI, owner's approval if the
  rule requires it; through `ameesh work queue add <lot>` by the orchestrator,
  which replaces the "merge go-ahead". A queued lot is frozen: its agent no
  longer pushes to it without removing it from the queue.
* **Order**: priority first (rule "merge first what unblocks the owner"), then
  lots without conflicts, then those whose conflicts are prepared, finally
  arrival. A lot with an unprepared conflict with a lot ahead of it moves back
  one rank, and its resolver is woken up: the conflict has become blocking.
* **Stacked trial**: before each merge, the target plus the lots ahead plus
  this one, resolutions replayed; an uncovered conflict removes the lot from
  the queue instead of blocking the following ones.

## 3.4 Merge that replays the resolutions

`ameesh work land <lot>`, launched by the runner (not by an agent turn) for the
head of the queue:

1. **lock per target**: a single merge at a time, which removes catch-up
   merges (29 at Nexlink on 10/10);
2. in the radar's clone: `fetch`, throwaway worktree on the fresh target,
   cache primed by this lot's resolution commits;
3. merge: `git merge --no-ff` of the freeze with the `ameesh-work: <id>` line
   (team that merges locally), or merge of the target into the PR branch
   (PR-based team); `rerere` replays; if a conflict remains, stop, the lot
   leaves the queue and its agent is woken up;
4. **project gates** on the merged tree (command declared per project:
   targeted tests, SQL suites, compilation); failure: same treatment;
5. push without forcing; PR: merge through the API once CI is green;
6. notice in the lot's thread (replayed resolutions, authors, gates), lot
   closed by the sweep (L118), radar recomputed for the queue, next lot.

A prepared merge thus requires **no agent turn**. Production operations and
release tags stay outside the queue: they follow the delivery rules of
ORCHESTRATEUR.md.

## 3.5 "Additive" conventions to impose in the prompts

For ameesh (CONTRIBUTING.md, agent prompts) and for Nexlink (its team
prompt):

1. **Add a file rather than modify a shared file**: one module, one test, one
   documentation page per topic.
2. **Logs as fragments**: one file per lot
   (`docs/design/log.d/2026-10-11-l160.md`), assembled by a script checked in
   CI; no more writing at the top of `log.md`. Likewise for Nexlink's knowledge
   log and roadmap.
   *Owner's decision of 11/10 (lot L160a): no fragments for ameesh, the
   hand-kept log `docs/design/log.md` is deleted; the history is git and the
   descriptions of merged PRs.*
3. **Generated indexes** from the cards' header (decisions index, studies
   index), never kept by hand.
4. **Lists**: one element per line, trailing comma, sorted alphabetically or
   within the topic's group, never several elements on one line
   (`ITEM_COLUMNS`, columns granted by `role-superviseur.sql`).
5. **Registries by discovery**: an alert, a command, a probe are declared in
   their own module, not in a central list (`main.py`, `mesh_cli.py`,
   `exploitation.py`, `notify.py`).
6. **Contract SQL files split** by table or by lot, assembled in order;
   translations split by namespace (one file per feature and per language).
7. **Numbers reserved with ameesh** at the start of the lot (`ameesh work
   reserve migration`, counter in the database, attached to the lot), never
   "the next one" nor a reservation commit on the shared branch; the radar's
   probe checks.
8. **No off-topic reformatting**: moving or reformatting shared code is a
   separate lot, announced by the radar and merged with an empty queue.
9. **Commit and push often**: the radar only sees commits.

# 4. Implementation lots

Letters rather than numbers, so as not to take numbers that other lots reserve
(precedent: L31b, L73b). Migrations: reserved at the start of the lot that
needs them (orchestrators' rule 5). Durations are agent work, review included;
the dates assume an available ameesh agent.

| Lot | Content | Depends on | Estimated duration | Proposed date |
|---|---|---|---|---|
| **L160a** — ameesh conventions and fragments | log as fragments (`log.d/`, assembly, CI check), generated decisions and studies indexes, `ITEM_COLUMNS` and lists one entry per line, `merge.conflictStyle=zdiff3` set by `ameesh doctor`, conventions in CONTRIBUTING.md and the prompts; done with an empty queue | — | 3 h | 12/10 |
| **L160b** — radar in observation mode | private clone per project (shared objects), pass after the branch sweep, files per lot, pairs, `merge-tree`, migrations probe, pairs table (1 migration), `ameesh work radar`, line in `ameesh projects`; no mail for 24 h, to calibrate | — | 5 h | 12/10 |
| **L160c** — radar mail | passive `radar` key (L125), mail linked to the lot on state change, wake-up only when the conflict blocks, lot thread, `work show`, `work watch` | L160b | 2 h | 13/10 |
| **L160d** — prepared resolutions | `ameesh work resolve`, resolution commits under `refs/ameesh/resolutions/`, priming by `rerere-train.sh`, `prepared` and `stale` states, choice of the resolver | L160b | 5 h | 13/10 |
| **L160e** — queue and merge by ameesh | queue per target (1 migration), order, stacked trial, `ameesh work queue`, `ameesh work land` (lock, replay, declared gates, push without forcing, PR through the API), `merge_blocked` alert, ORCHESTRATEUR.md without "merge go-ahead" | L160d | 6 h | 14/10 |
| **L160f** — Nexlink | conventions in its prompt, translations and reservations registry split, log as fragments, declared gates, ameesh queue instead of the merge go-ahead; one-day pilot | L160e, approval of the Nexlink orchestrator | 4 h and 1 pilot day | 15/10 |
| **L160g** — syntactic driver | mergiraf replayed on the 103 measured conflicting merges; adoption per language (Python, TypeScript, JSON) only if no wrong resolution | — | 2 h | 13/10 |
| **L160h** — gain measurement | this study's measurement scripts in `scripts/` (replay, reflogs, mail), report at D+7 | L160e | 1 h | 19/10 |

Total: 28 h of agent work and one pilot day. Recommended order: L160a and
L160b on the same day, then L160c, L160d and L160g, then L160e, then L160f;
L160h one week later.

## Expected gain

Baseline: the measurements above, redone by L160h with the same scripts.

| Measure | Before (10/10) | After L160a | After L160e (L160f for Nexlink) |
|---|---|---|---|
| ameesh: PRs needing a resolution by hand at merge time | 11 out of 18 | about 7 (−40%: merges that had only additions disappear, 4 out of 10 that night, 9 out of 27 since 05/10) | 2 at most (unprepared conflicts) |
| ameesh: resolutions by hand over a night of this kind | 19 | about 11 | 3 at most at merge time; the others prepared as work goes on |
| Nexlink: catch-up merges | 29 in 16 h | unchanged | 0 (lock per target) |
| Nexlink: delay from go-ahead to develop | median 39 min, 3rd quartile 76 min | unchanged | median 10 min, 3rd quartile 25 min (gates and push) |
| Nexlink: messages about merging | 382 out of 1,345 in 4 h (28%) | unchanged | fewer than 150 (review reports remain) |
| Nexlink: orchestrator's merge messages | 76 in 4 h | unchanged | fewer than 10 (queue entry, exceptions) |
| Radar cost | — | — | less than 10 s of computation every 5 min on the largest project |

Assumptions: half of the conflicting blocks are additions, and the conventions
remove them instead of resolving them; the remaining conflicts are prepared at
their resolver's next turn, before the other lot merges, in nine cases out of
ten; merging a lot at the head of the queue takes the time of its gates (8 min
measured). Semantic conflicts do not decrease; they keep being caught by the
gates on the merged tree, now in the actual merge order.

## Safeguards

* A replayed resolution can be wrong: project gates on the merged tree at each
  merge, resolution attributed to its author in the notice, reviewable commit.
* The radar never merges anything: it computes in its clone and warns.
* A team that has not declared its gates keeps merging by hand: the queue
  orders, the agent merges.
* A merge by hand remains possible in an emergency (human, orchestrator): it
  goes through, and the radar recomputes the queue.
* The public ameesh repository only receives, as resolution refs, code already
  made public by the PR branches.

# Questions for the owner

1. Does ameesh keep GitHub's squash merge? Merging by ameesh then integrates
   `main` into the PR branch before merging it through the API. GitHub's
   queue, available for this public repository, is not needed as long as CI
   takes less than 5 min.
2. Does Nexlink entrust the execution of its merges to ameesh (gates launched
   by the runner, not by an agent)? Without that, L160f is limited to the order
   and the prepared resolutions.
3. Do the agents of other hosts push their lot branch at the end of every turn
   that created a commit? Without that, the radar only sees their work at
   freeze.
