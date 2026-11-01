-- SPDX-License-Identifier: AGPL-3.0-only
-- #5140: opaque credentials; the admission owns identity and expiry.
set local lock_timeout = '5s';
create table sandbox_relay_tokens (
    token_sha256 text primary key check (token_sha256 ~ '^[0-9a-f]{64}$'),
    turn_id text not null references sandbox_turns(turn_id) on delete cascade,
    host text not null,
    created_at timestamptz not null default clock_timestamp()
);
create index sandbox_relay_tokens_turn on sandbox_relay_tokens(turn_id);

-- L60's unique spend_key is unchanged. Relay retries have their own key.
alter table turn_costs add column relay_call_id text;
create unique index turn_costs_relay_call on turn_costs (turn_id, relay_call_id)
    where source = 'relay';
