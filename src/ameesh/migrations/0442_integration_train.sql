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

-- Queue identity evidence is written only by allocation/withdrawal operations.
-- Free-text work_item_events are never admission receipts, including older
-- notes with the same apparent format: do not import them into this table.
create table if not exists integration_train_queue_receipts (
    id bigserial primary key,
    work_item_id bigint not null references work_items(id) on delete cascade,
    generation bigint not null check (generation > 0),
    frozen_id bigint,
    kind text not null check (kind in ('packet', 'removed')),
    created_at timestamptz not null default clock_timestamp(),
    unique (work_item_id, generation, kind)
);
create index if not exists integration_train_queue_receipts_freeze
    on integration_train_queue_receipts(work_item_id, frozen_id, id);
