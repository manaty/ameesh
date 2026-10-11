-- SPDX-License-Identifier: AGPL-3.0-only
-- 0056_work_item_measures — actual duration of a delivered lot split by
-- phase, and the size of its change measured on the merged diff (lot L157c).
--
-- L157 measures one actual duration per lot (merge − start). L157b asks the
-- estimators to estimate by phase (agent work, CI runs, waiting for review
-- and merge, human steps) but nothing measured the phases. This table holds,
-- for each delivered lot, one measure taken after its merge:
--
-- * the phases, in minutes of wall-clock time, which add up to the total
--   (start → merge). Each instant belongs to one phase only, by priority:
--   human (state `waiting_human`) > review (from a freeze to the next return
--   to `build`, and state `qa`) > CI (a CI run of the lot's branch) > agent
--   (the rest). `ci_minutes` is NULL when the CI runs could not be read
--   (no branch, no `gh`): that time is then counted as agent time.
-- * the size of the change on the merged diff (first parent of the merge
--   commit → merge commit): files, inserted and deleted lines, and the
--   measured size class (s, m, l) to compare with the declared
--   `work_items.change_size` (copied here at measuring time). NULL when no
--   repository of the measuring host holds the merge commit.
--
-- Written by the runner after its branch sweep (`lot_measures.measure_pending`)
-- and by `ameesh work measure`; read by `work show` and by the auditor.

create table if not exists work_item_measures (
    work_item_id    bigint primary key references work_items(id) on delete cascade,
    measured_at     timestamptz not null default now(),
    started_at      timestamptz not null,
    merged_at       timestamptz not null,
    total_minutes   numeric(10, 1) not null check (total_minutes >= 0),
    agent_minutes   numeric(10, 1) not null check (agent_minutes >= 0),
    ci_minutes      numeric(10, 1) check (ci_minutes >= 0),
    review_minutes  numeric(10, 1) not null check (review_minutes >= 0),
    human_minutes   numeric(10, 1) not null check (human_minutes >= 0),
    ci_runs         integer check (ci_runs >= 0),
    merge_sha       text,
    diff_files      integer check (diff_files >= 0),
    diff_insertions integer check (diff_insertions >= 0),
    diff_deletions  integer check (diff_deletions >= 0),
    measured_size   text check (measured_size is null or measured_size in ('s', 'm', 'l')),
    declared_size   text check (declared_size is null or declared_size in ('s', 'm', 'l'))
);

comment on table work_item_measures is
  'L157c: actual duration of a delivered lot split by phase (agent, CI, review, human) and size of its merged diff, measured once after the merge.';
