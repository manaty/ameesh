# Lot footprints and non-overlapping scheduling (#3185, part 3)

A lot's **footprint** is the set of files or modules (directories) it is
expected to touch, relative to the root of its repository (`app`). The
scheduler never runs two lots of the same repository whose footprints
overlap: the second one waits for the first one's merge, and so starts on the
updated base. A footprint that is too broad costs one lot of waiting; one that
is too narrow (or unknown) does not hold anything back, so the diff refresh
matters.

```sh
ameesh work footprint 212                      # show it, and the lots in progress it overlaps
ameesh work footprint 212 --set src/ameesh/backlog.py docs
ameesh work footprint 212 --estimate           # re-estimate from the title and body
ameesh work footprint 212 --refresh            # refine from the branch's actual diff
ameesh work footprint 212 --clear              # drop the estimate (diff rows stay)
ameesh work footprint 212 --clear-diff         # drop wrong diff rows (pilot gesture)
```

## Sources

The footprint is the union of three sources (`work_item_footprints`, 0502):

- `intake`: estimated at creation from the paths cited by the title and body
  (the same detection as the intake neighbors), for **every** creation path:
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

## Overlap

Two paths overlap when they are equal or one is a parent directory of the
other: `src/a` overlaps `src/a/b.py`, not `src/ab.py`. Paths of two different
repositories never overlap. A lot without any footprint row has no known
footprint and is not held.

## Scheduling rule

A lot **occupies** its footprint from the moment it is assigned or started
until it is merged, promoted or closed: `build`, `qa`, `waiting_human` and
`blocked` lots occupy it, and so does a `build` lot returned to the queue
(its branch exists).

An `intake` lot whose footprint overlaps an occupying lot of the same
repository:

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
