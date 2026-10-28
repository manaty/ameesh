# Lot footprints and non-overlapping scheduling (#3185, part 3)

A lot's **footprint** is the set of files or modules (directories) it is
expected to touch, relative to the root of its repository (`app`). The
scheduler never runs two lots of the same repository whose footprints
overlap: the second one waits for the first one's merge, and so starts on the
updated base. A wrong footprint costs one lot of waiting, never a conflict.

```sh
ameesh work footprint 212                      # show it, and the lots in progress it overlaps
ameesh work footprint 212 --set src/ameesh/backlog.py docs
ameesh work footprint 212 --estimate           # re-estimate from the title and body
ameesh work footprint 212 --refresh            # refine from the branch's actual diff
ameesh work footprint 212 --clear              # drop the estimate (diff rows stay)
```

## Sources

The footprint is the union of three sources (`work_item_footprints`, 0502):

- `intake`: estimated by `ameesh work add` from the paths cited by the title
  and body (the same detection as the intake neighbors). With a local clone of
  the repository, a bare file name becomes its path when it is unique. A cited
  path absent from the repository is kept: it is often the file to create.
- `manual`: set by `--set`; it replaces the intake estimate.
- `diff`: the files the lot's branch actually changes since its base on the
  target (`git diff --name-only target...head`). Refreshed when the lot is
  frozen (frozen head) and by `--refresh` (branch head).

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

Footprints set or refreshed on lots that are *already* in progress are not
retroactive: `ameesh work footprint` shows the overlap, and the pilot decides.
