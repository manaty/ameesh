-- SPDX-License-Identifier: AGPL-3.0-only
-- #5751 : boîte de réception durable des refus natifs, sans verdict de test.
set local lock_timeout = '5s';
create table if not exists ci_disk_refusals (
    repo text not null,
    host text not null,
    event_id text not null,
    payload jsonb not null,
    message_id bigint,
    seen_at timestamptz not null default now(),
    primary key (repo, event_id)
);
