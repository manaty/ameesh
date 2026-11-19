-- SPDX-License-Identifier: AGPL-3.0-only
set local lock_timeout = '5s';
drop index if exists agent_mailbox_pre_act_withdrawn_idx;
drop index if exists agent_mailbox_pre_act_recent_idx;
