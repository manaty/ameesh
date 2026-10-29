-- SPDX-License-Identifier: AGPL-3.0-only
-- #4174: the project plan reads pending decisions once, without scanning
-- every unread mailbox entry. Additive; bounded lock acquisition.
set local lock_timeout = '5s';
create index if not exists agent_mailbox_pending_decisions_idx
    on agent_mailbox (btrim(work_item_id))
    where delivered_at is null and kind = 'request'
      and payload->'decision' is not null;
