-- SPDX-License-Identifier: AGPL-3.0-only
-- 0413_agent_mailbox_work_item_index — the last activity of an open lot (L521, lot #521).
--
-- `open_lots_activity` (the stale/idle lots read by `alerts`) joins
-- `agent_mailbox` on `work_item_id = work_items.id::text`, one scan per open
-- lot. Without an index on that column, Postgres scanned the whole mailbox
-- (13 311 rows) once per lot: EXPLAIN ANALYZE measured 735 ms and 191 053
-- shared buffers for 104 open lots, and `alerts` ran that query up to four
-- times per command. `actions` gets the same index for its own lateral join
-- (`work_item = work_items.id`), empty today but growing with the gated
-- actions.
--
-- Partial on `is not null`: mail without a lot and actions without a work item
-- are never looked up this way, and the index stays small.
--
-- A plain build, not `CONCURRENTLY`: `migrate()` runs every migration inside
-- one transaction (`db.script`), and Postgres refuses a concurrent build
-- there. The mailbox is busy, so wait at most 5 s for the table's lock: if a
-- writer holds it longer, the migration fails and stays pending instead of
-- queueing every `agent_mailbox` writer behind the build.
set local lock_timeout = '5s';

create index if not exists agent_mailbox_work_item_idx
  on agent_mailbox (work_item_id) where work_item_id is not null;

create index if not exists actions_work_item_idx
  on actions (work_item) where work_item is not null;
