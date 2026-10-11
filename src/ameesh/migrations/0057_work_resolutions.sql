-- SPDX-License-Identifier: AGPL-3.0-only
-- 0057_work_resolutions — prepared conflict resolutions between two lots
-- (lot L160d, study L160 § 3.2).
--
-- When the radar finds two lots whose branches merge with a conflict, one of
-- them (the resolver) prepares the resolution ahead of the merge:
-- `ameesh work resolve <lot> --with <other>` merges the other branch into a
-- throwaway worktree of its own branch, the agent resolves, and the
-- resolution commit R(lot, other) is pushed under
-- `refs/ameesh/resolutions/<lot>/<other>` (or the branch
-- `ameesh/resolve/<lot>-<other>` when the host refuses refs outside
-- `refs/heads`). The lot branch itself never changes.
--
-- One row per ordered pair: the latest resolution prepared by `work_item_id`
-- for its conflict with `other_item_id`. Its validity (`prepared`, `stale`)
-- is not stored: the radar replays the pair at each pass, `rerere` primed
-- from the resolution commits, and writes the state to its host journal.

create table if not exists work_resolutions (
    work_item_id  bigint not null references work_items (id) on delete cascade,
    other_item_id bigint not null references work_items (id) on delete cascade,
    -- the resolution commit: first parent = tip of the lot branch, second
    -- parent = tip of the other branch, both as merged
    commit_sha    text not null check (commit_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
    lot_tip       text not null check (lot_tip ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
    other_tip     text not null check (other_tip ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
    -- where it was pushed (NULL: kept in the local repository only)
    ref           text,
    -- the paths that were in conflict
    files         text[] not null default '{}',
    created_by    text not null,
    created_at    timestamptz not null default now(),
    primary key (work_item_id, other_item_id),
    check (work_item_id <> other_item_id)
);

create index if not exists work_resolutions_other_idx on work_resolutions (other_item_id);
