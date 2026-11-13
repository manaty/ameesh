-- SPDX-License-Identifier: AGPL-3.0-only
-- #7066: names are an index only; authority is re-read from the signed decision.
create table if not exists account_turn_exceptions (
    decision_id bigint primary key references agent_mailbox(id),
    agent text not null,
    harness text not null,
    account text not null,
    registered_at timestamptz not null default now(),
    consumed_at timestamptz,
    consumed_host text
);
create index if not exists account_turn_exceptions_pending_idx
    on account_turn_exceptions(agent, harness) where consumed_at is null;
