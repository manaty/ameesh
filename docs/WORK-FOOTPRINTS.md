# Lot footprints and non-overlapping scheduling (#3185, part 3)

A lot's **footprint** is the set of files or modules (directories) it is
expected to touch, relative to the root of its repository (`app`). The
scheduler never runs two lots of the same repository whose footprints
overlap: the second one waits for the first one's merge, and so starts on the
updated base. A footprint that is too broad costs one lot of waiting; one that
is too narrow (or unknown) does not hold anything back, so the diff refresh
matters.

```sh
ameesh work footprint 212                      # show it, its impact analysis, the overlaps
ameesh work footprint 212 --set src/ameesh/backlog.py docs
ameesh work footprint 212 --set .              # the whole repository (pilot gesture)
ameesh work footprint 212 --estimate           # rerun the impact analysis
ameesh work footprint 212 --refresh            # refine from the branch's actual diff
ameesh work footprint 212 --clear              # drop the estimate (diff rows stay)
ameesh work footprint 212 --clear-diff         # drop wrong diff rows (pilot gesture)
```

## Sources

The footprint is the union of three sources (`work_item_footprints`, 0502):

- `intake`: estimated at creation by the **impact analysis** (#4818, below),
  for **every** creation path:
  `ameesh work add`, `agent-mail --new-lot`, incident, fingerprint, review-lot,
  packet carrier, improvement backlog and basket lines. With a local clone of
  the repository, a bare file name becomes its path when it is unique. A cited
  path absent from the repository is kept: it is often the file to create.
  Paths are relative to the repository root; an absolute path, `~` or a drive
  letter is refused rather than silently made relative.
- `manual`: set by `--set` (a pilot gesture: human, orchestrator or design
  agent, since it can lift a wait); it replaces the intake estimate. While a
  manual footprint exists, `--estimate` writes nothing (it would add intake
  rows under the correction); `--clear` then `--estimate` re-estimates.
- `diff`: the files the lot's branch actually changes since its base on the
  target (`git diff --name-only --no-renames target...head`: a renamed file
  keeps both its old and new paths). The base is the lot's `branch_target`,
  else the target its project declares (`radar.projects.<p>.target`: `develop`
  for Nexlink), else the repository's default branch (`origin/HEAD`). A lot
  without an explicit target is never diffed against the forge's default
  branch alone when the project declares an integration target (#4733: `main`
  gave 354 paths for the 29 real ones, and `--refresh` recomputed them).
  Refreshed when the lot is frozen (frozen head; a failure is a warning, never
  a failed freeze) and by `--refresh` (branch head). `--refresh` reads the
  target's **published** tip first (read-only: `ls-remote` then the exact
  commit, no ref moved; a mute forge falls back to the clone's ref) and says
  when the base is only the clone's last fetch, instead of passing it for the
  target of the day.

Without a local clone, a bare file name cited at intake (`foo.py`) stays at the
repository root and may be narrower than the real path (`src/foo.py`):
`--set` or the diff refresh corrects it.

A `diff` computed on a wrong base is replaced by the next `--refresh` (or
freeze) once the target is right. When the branch or the target can no longer
be read, a pilot drops the stale rows with `--clear-diff`: the intake/manual
estimate stays, the overlap disappears with the false files. `--set` and
`--clear` never touch the diff rows.

Every change writes one line in the lot's journal (`Footprint (source): …`).

## Impact analysis (#4818)

Before dispatch, every lot gets a fast, **read-only** and deterministic (no
model) analysis of the files it will probably touch, with a confidence per
path (`impact_analysis.py`):

- `high`: a path cited by the title or body (the intake neighbors detection).
  With a local clone of the repository, a bare file name becomes its path when
  it is unique. A cited path absent from the repository is kept: it is often
  the file to create.
- `medium`: a cited identifier (`snake_case`, `CamelCase`, `module.function`,
  a command such as `sync-branches`) that `git grep` finds in at most 3 files
  outside the tests. Otherwise only its definitions count (`def`, `class`,
  `function`…).
- `weak`: a title word that names a file (`footprint` →
  `work_footprints.py`). It is journaled only and never written to the
  footprint: a guess holds no lot back.

The budget is bounded twice: 3 s shared by every git call, and 16 git calls.
Every estimate writes **one line** in the lot's journal (`Impact analysis: …`,
known or not, with its time and git calls). The latest analysis is kept in
`work_item_impact_analyses` (0519), and `ameesh work footprint <lot>` shows it.
A manual footprint (`--set`) always wins: neither `--estimate` nor the retries
rewrite it.

**Unknown footprint.** When the analysis finds no `high` or `medium` path (no
cited path or identifier, no clone, budget exhausted), the repository's
policy decides:

- `open` (the default when no policy is set): the lot holds nothing, the rule
  before #4818;
- `repository`: the lot carries the marker `.` (the whole repository), which
  overlaps every path of the repository. While it is in progress it holds
  every other lot of the repository, and while it is waiting every lot in
  progress holds it.

The analysis (git) runs outside any lock; the marker is decided where the
analysis is written — the creation, `--estimate`, the runner's retry — on
the policy read under the guard's lock, the lock of a policy change: a
switch between the computation and the write is never lost, in either
direction. A `--set` that lands while an estimate is being computed wins: the
stale estimate writes nothing. A lot whose branch diff is known never gets
the marker back: the diff wins over any estimate, and its unknown analysis is
closed (not retried).

**Lots that are not code** — a packet vehicle (`integration/**`), a question
to the pilot (`source = decision`), a roadmap-cutting lot
(`file-decoupage:…`) — never get the marker, are neither counted nor held
by the in-flight cap, and are not refreshed at push. An improvement backlog
lot is code, taken like any other: it follows the common rule. A basket
line takes the marker too, united with the earlier lines' paths, and a known
line does not clear the unknown analysis of an earlier one. On every creation
path the policy is read and the marker written in the creation's own
transaction, under the guard's lock: no take sees the lot without it.

A change of the unknown footprint policy applies at once, in the same
transaction and under the guard's lock, to the open lots of the repository:
`repository` puts the marker on every open lot without any footprint row
(unknown under `open`, or created before #4818; packet vehicles excepted) and
opens an unknown analysis for the runner to retry; `open` removes the
`intake` markers (a `.` set by `--set` stays). Each lot gets a journal line.

The runner retries an unknown analysis every 30 min (up to 5 lots per pass),
on a host that has a clone of the repository. The first diff refresh
replaces the marker: a branch's real diff is the known footprint.

## Refresh at the first push (#4818)

Each runner, in its branch sweep, refreshes the `diff` rows of the lots in
progress assigned to its host's agents as soon as the branch is **pushed**:
when `origin/<branch>` exists in the host's clone (a `git push` updates it
locally, with no call to the forge) and no diff was read, or it was read on
another head. The base is the clone's ref of the target. At most 5 lots are
refreshed per pass, among at most 100 candidates read through a cursor: the
next pass resumes after the last lot inspected and wraps at the end, so a
first push is never stuck behind lots already up to date. The diff refresh on freeze and on `--refresh` is
unchanged.

## Repository policy (#4818)

```sh
ameesh work footprint-policy                         # every policy set
ameesh work footprint-policy tpk --unknown repository
ameesh work footprint-policy tpk --max-in-flight 4   # or --no-cap
ameesh work footprint-policy tpk --shared CHANGELOG.md src/locales/index.ts  # or --no-shared
```

A pilot gesture (human, orchestrator or design agent), journaled in the
events (`footprint.policy`, before and after), and stored in
`repository_footprint_policies` (0519) so that the SQL of the guard reads it
under its lock:

- `--unknown open|repository`: the unknown footprint policy (above);
- `--max-in-flight N`: at most N lots of the repository occupy their
  footprint at once. Lots that are not code (below) are neither counted nor
  held. A free lot of a repository at its cap is held like an overlap:
  it is not offered to idle agents, `work assign` refuses it (`in-flight cap
  (N/N)`), and an assigned creation is created unassigned with a warning;
- `--shared PATH…`: the files or directories whose merges are trivial
  (changelog, locale index…). They are ignored for overlap on both sides, and
  a path under a shared directory is ignored too. The marker `.` is never
  shared.

## Overlap

Two paths overlap when they are equal or one is a parent directory of the
other: `src/a` overlaps `src/a/b.py`, not `src/ab.py`. The marker `.`
overlaps everything. A shared path of the repository's policy overlaps
nothing. Paths of two different repositories never overlap. A lot without
any footprint row has no known footprint and is not held.

## Scheduling rule

A lot **occupies** its footprint from the moment it is assigned or started
until it is merged, promoted or closed: `build`, `qa`, `waiting_human` and
`blocked` lots occupy it, and so does a `build` lot returned to the queue
(its branch exists).

An `intake` lot whose footprint overlaps an occupying lot of the same
repository, or whose repository has reached its in-flight cap:

- is not offered to idle agents (`unassigned_lots`, L165), and the take
  re-checks the rule under the dependency-graph lock after locking the lot;
- is shown as waiting by the project dispatch plan (`ameesh work plan
  --project`), the same rule as the dispatcher;
- is refused by `ameesh work assign`, which names the overlapping lots and
  paths. Nothing is written. Reassigning a lot that already occupies its
  footprint is not affected;
- cannot be *created* assigned: `ameesh work add --assignee` writes the intake
  footprint in the creation transaction, and an overlap rolls it back. The
  command then creates the lot unassigned, with a warning naming the
  overlapping lots; it waits, and is taken after their merge.

All three paths take the same advisory lock before checking, so two
concurrent attributions or creations of overlapping lots decide one after
the other. When the occupying lot merges, the waiting lot becomes takeable at
the next pass.

Footprint writes take the same lock, so a footprint never changes between
a check and the attribution it guards. Footprints set or refreshed on lots
that are *already* in progress are not retroactive: `ameesh work footprint` shows the overlap, and the pilot decides.

**A pairwise disjoint selection.** Two *free* lots that overlap each other
are both startable for the guard. The project dispatch plan (`ameesh work
plan --project`, and the dispatcher that reads it) walks the lots in dispatch
order and keeps a set of ready lots whose footprints are pairwise disjoint. A
later lot that overlaps one already selected waits, with `footprint overlaps
#N, dispatched first`. The in-flight cap counts the selected lots on top of
those already in progress (`repository in-flight cap N reached with #…`).

## SLO and dispatch (#4561)

A held `intake` lot is not late: while an occupying lot overlaps its
footprint, the SLO steps "reported → assigned" and "ready → started" are
suspended, because `ameesh work assign` could not take it. The wait is dated
on the lot by a journal note, `en attente d'empreinte : #12, #34`; after the
last blocker merges (or closes), both clocks restart from that merge, never
from the lot's creation.

`ameesh debit` never lists such a lot as startable. It reports it apart, in
`footprint_lots`, with the blocking lot and its state. When the blocker has
not moved for 2 h, the due gesture is on it — its verdict or its package —
not on an assignment.

The wait for the in-flight cap is a footprint wait: every occupying lot of
the repository holds the waiting lot (`reason: cap`). `ameesh debit` says so
(`attend le plafond de lots en vol du dépôt`).

## A lot that occupies without moving (#4818)

The guard never releases a footprint by itself. When a lot that holds other
lots (by its footprint or by the cap) is stale (`stale_lot` alert, to the
pilots), the alert names the lots it holds and the due gesture: move it
forward, narrow a wrong footprint (`--set`, or push the branch so that the
diff replaces the estimate), or return it to the queue (`ameesh work return`).
