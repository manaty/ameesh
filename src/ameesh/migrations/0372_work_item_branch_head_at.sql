-- SPDX-License-Identifier: AGPL-3.0-only
-- 0372_work_item_branch_head_at — when the sweep saw `branch_head` (L118d,
-- lot #355).
--
-- After a partial merge (`ameesh-work-partial`), only work the merge sweep
-- saw on the lot's branch AFTER that merge, then merged into the target,
-- lifts the partial marker. `branch_head` alone cannot tell: a head seen
-- before the merge, merged later by another branch, looked like new work
-- (#346 review r1). `branch_head_at`: when the sweep recorded `branch_head`
-- (database clock); null for a head recorded before this migration, which
-- therefore never lifts a partial marker.

alter table work_items add column if not exists branch_head_at timestamptz;
