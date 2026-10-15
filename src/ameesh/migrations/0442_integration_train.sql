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
