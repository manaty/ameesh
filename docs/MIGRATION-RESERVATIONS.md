# Migration reservations (L122, #711)

Reserve numbers before writing migration files. The existing command remains
compatible, including its single-lot JSON object and repository-wide scan:

```sh
git fetch origin
ameesh work migration 711 --repo /path/to/clone
```

Landing consumers can reserve several symbolic slots in one transaction. Fetch
the target first, then pass its full commit SHA. Repeat `--slot` in the required
migration order; numbers are contiguous and the entire batch either succeeds
or rolls back.

```sh
git fetch origin
sha=$(git rev-parse origin/develop)
ameesh work migration 702 --repo /path/to/clone \
  --target-branch develop --target-sha "$sha" \
  --slot schema --slot backfill --json
```

The result is an array, in the requested slot order:

```json
[
  {"lot":702,"slot":"schema","id":21,"number":377,"project":"nexlink",
   "target":{"repo":"github.com/manaty/nexlink","branch":"develop"},
   "targetSha":"0123456789abcdef0123456789abcdef01234567","status":"reserved"},
  {"lot":702,"slot":"backfill","id":22,"number":378,"project":"nexlink",
   "target":{"repo":"github.com/manaty/nexlink","branch":"develop"},
   "targetSha":"0123456789abcdef0123456789abcdef01234567","status":"reserved"}
]
```

`slot` is the symbolic name used by a consumer's `@MIG(slot)@` marker. `id` is
the numeric database record identity; `number` is the migration number.
Slots are unique per lot. `default` belongs to the legacy single reservation.
`target.repo` is the canonical origin from `merge_queue.source_of`, with GitHub
paths lowercased and nondefault ports retained. It is distinct from the local
clone path. Branches omit `origin/`, `refs/heads/` and `refs/remotes/origin/`.

The allocator uses the key `(target.repo, target.branch)`. `project` is
context data: projects sharing a repository and target share their numbers. The first
number is `max(migrations on the target at targetSha, active reservations of
the same key) + 1`. Source branches, other branches, the worktree and installed
package do not contribute to this floor. Legacy reservations are projected
into the same records and the legacy allocator also reads active slot numbers.
There is no independent migration counter.

The target is inspected in the provided clone: its `origin/<branch>` ref takes
precedence over the local branch. The CLI does not fetch or attest remote
freshness. A landing coordinator must fetch, reserve and recheck the remote
target before applying a batch. A mismatch of the supplied SHA and the clone's
target is refused. A target ref moving during allocation rolls the transaction
back. Reservations at an earlier SHA of this key become `released` when a new
SHA is used for reservation, but only if the original SHA is an ancestor
of the new target and the number is absent from its migration directory.
When the ancestor's number is already present, the record becomes `landed`,
including when another lot reserves before the landing caller reports success.
Reservations whose SHA is not an ancestor remain active and contribute to the
maximum; a stale or diverged clone cannot release them. Requalifying one's own
slot to a non-descendant is refused. Missing history also refuses allocation,
requiring a fetch before ancestry can be proved. Requalification retains each
slot's technical identity.

Replaying the same batch at the same SHA preserves its numbers and creates no
new journal entries. A batch containing already reserved slots must replay
those same contiguous slots in order; partial replay mixed with new slots is
refused. A new independent batch may name other slots. Allocation requires an
open lot with a project and a known migration layout.

Read or transition the slots explicitly:

```sh
ameesh work migration 702 --list --json
# Optional --slot filters the read-only array.
ameesh work migration 702 --repo /path/to/clone \
  --slot schema --slot backfill --target-branch develop \
  --target-sha "$sha" --reservation-id 21 --reservation-id 22 --release --json
# After landing, pass the new target commit containing the numbered files.
landed_sha=$(git rev-parse origin/develop)
ameesh work migration 702 --repo /path/to/clone \
  --slot schema --slot backfill --target-branch develop \
  --target-sha "$landed_sha" --reservation-id 21 --reservation-id 22 --landed --json
```

A release matches the original reservation SHA. Landing requires that the
reserved SHA is an ancestor of the supplied target commit and that each
reserved number appears in its migration directory. The consumer remains
responsible for checking the migration contents. Repeated lifecycle transitions
are idempotent. Released slots can reserve again; landed slots retain their
history and cannot be released or reused. Explicit cleanup and landing remain
possible after the lot is closed or merged. The reservation stays active across
freeze, review and lot-state changes until explicitly landed, released, or
invalidated by target requalification.

Migration `0419_migration_reservation_slots.sql` adds the reservation table,
a partial unique index on reserved numbers and a trigger for the old
`work_items.migration` projection. Existing legacy records have a null
`targetSha`; they appear under `default` and use `branch_target` when recorded.
When no target was recorded, `target.branch` stays null: SQL cannot determine
the Git default branch. That reservation conservatively occupies its number
on every target of the repository until a target is set or the legacy record
is released/landed. A SQL guard serializes this overlap check with named slots,
so an absent project or target cannot bypass uniqueness. They keep the original CLI lifecycle. New named
slots always have a pinned SHA and use the explicit lifecycle above.

The named allocator takes a repository advisory lock (unknown legacy targets
span branches), then the target lock, then affected lot rows in id order.
Ancestry and lifecycle observations are made under the repository and target
locks, before lot rows are edited.
The upgraded legacy allocator also takes the repository lock before its lot
update. The named path retries a deadlock victim from clients using the older
lock order; every failed attempt rolls its records and journal back.

During rollout, install the additive schema and upgrade/restart legacy writers
before enabling named reservations. Pre-0419 code does not see named slots or
recognize the compatibility guard's constraint in its retry loop: it may receive
a uniqueness error or be a deadlock victim. The database rejects the conflicting
write, so no duplicate is accepted. Enabling the new code first on an unmigrated
schema fails because the reservation table is missing.
