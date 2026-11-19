-- SPDX-License-Identifier: AGPL-3.0-only
-- Pre-act reads all unread mail for the actor, all mail since its start, and
-- all withdrawn decisions (including old messages withdrawn recently).
-- The existing recipient/unread index covers the first group.
set local lock_timeout = '5s';
create index agent_mailbox_pre_act_recent_idx on agent_mailbox (created_at, id);
create index agent_mailbox_pre_act_withdrawn_idx on agent_mailbox (id)
    where payload->'decision'->>'state' = 'withdrawn';
