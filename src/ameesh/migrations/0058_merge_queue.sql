-- SPDX-License-Identifier: AGPL-3.0-only
-- 0058_merge_queue — merge queue per target (lot L160e, study L160 § 3.3).
--
-- A lot enters the queue of its target when it is ready: frozen, review
-- verdict `ok` on the frozen commit, CI green when it has a PR
-- (`ameesh work queue add <lot>`, which replaces the "merge light" given by
-- hand). At each pass, the radar orders each queue (priority, then lots
-- without conflict, then lots whose conflicts are prepared, then arrival)
-- and replays it stacked in its private clone: the target, plus the lots
-- ahead, plus this one, prepared resolutions replayed by `rerere`. A lot
-- whose conflict is not covered, or whose branch moved past its freeze,
-- leaves the queue (`ejected`) instead of blocking the ones behind.
--
-- One row per lot (a lot has one target). `landed` rows stay as history
-- until the lot is closed.

create table if not exists merge_queue (
    work_item_id bigint primary key references work_items (id) on delete cascade,
    target       text not null,
    -- the frozen commit that was queued: the branch must not move past it
    frozen_sha   text not null check (frozen_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
    state        text not null default 'queued'
                 check (state in ('queued', 'landing', 'landed', 'ejected')),
    -- 1 = head of the queue; NULL until the radar's first pass, or once ejected
    rank         integer,
    -- L160e2 (`land`, team with PRs): the merge commit pushed to the lot
    -- branch, whose CI is awaited before the PR is merged
    landing_sha  text check (landing_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
    -- why the lot is where it is (order, ejection)
    reason       text not null default '',
    queued_by    text not null,
    queued_at    timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create index if not exists merge_queue_target_idx on merge_queue (target, state);

-- One merge at a time per target (L160e2: `ameesh work land`): a lease, as
-- for agents (registry), fenced by its epoch so that a dead executor frees it.
create table if not exists merge_locks (
    target      text primary key,
    owner       text,
    epoch       bigint not null default 0,
    expires_at  timestamptz
);
