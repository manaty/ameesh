-- SPDX-License-Identifier: AGPL-3.0-only
-- #1467: durable train intent, shared across executor restarts/hosts.
-- The key is the existing land.lock_key; its merge_locks row fences writes.
-- No new grants, SECURITY DEFINER function or mediated API operation.
create table if not exists integration_trains (
    target_key text primary key references merge_locks(target),
    revision bigint not null default 1 check (revision > 0),
    document jsonb not null check (jsonb_typeof(document) = 'object'),
    updated_at timestamptz not null default clock_timestamp()
);

-- One immutable receipt per grant. A withdrawal never deletes its history.
-- All transitions lock merge_locks first, as does the train ledger.
create table if not exists integration_train_fires (
    id bigserial primary key,
    target_key text not null references merge_locks(target),
    plan_id text not null,
    document jsonb not null check (jsonb_typeof(document) = 'object'),
    state text not null check (state in ('active', 'revoked', 'promoting', 'landed')),
    changed_by text not null,
    owner text,
    epoch bigint,
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp()
);
create index if not exists integration_train_fires_plan
    on integration_train_fires(target_key, plan_id, id desc);
