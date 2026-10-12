-- SPDX-License-Identifier: AGPL-3.0-only
-- 0372_work_item_branch_head_at — when the sweep saw `branch_head`, and when
-- it first saw the lot's latest partial merge (L118d, lot #355).
--
-- After a partial merge (`ameesh-work-partial`), only work the merge sweep
-- saw on the lot's branch AFTER that merge, then merged into the target,
-- lifts the partial marker. `branch_head` alone cannot tell: a head seen
-- before the merge, merged later by another branch, looked like new work
-- (#346 review r1). Nor can the merge's git date, which its author sets
-- (#346 review r2). Both instants come from the database clock:
-- `branch_head_at`, when the sweep recorded `branch_head` (null for a head
-- recorded before this migration, which never lifts a partial marker);
-- `partial_merge` / `partial_seen_at`, the latest partial merge commit and
-- when the sweep first saw it.

alter table work_items add column if not exists branch_head_at timestamptz;
alter table work_items add column if not exists partial_merge text;
alter table work_items add column if not exists partial_seen_at timestamptz;
