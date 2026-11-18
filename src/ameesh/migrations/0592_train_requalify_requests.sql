-- SPDX-License-Identifier: AGPL-3.0-only
-- #7751: reviewed canon requalification requested from any host, applied by
-- the live train service lease holder with the same review guards as
-- `train_requalify_review.qualify` (`reviewed` path).
--
-- The request takes no lease and writes no configuration: it only carries the
-- exact reading (project, pin, gate), the cited milestone opinions and the
-- trace lot. The holder applies it under its own lease and its own
-- configuration; a request whose pin or gate moved is refused by name.
-- Additive: new table, existing grants already cover it (role-executor).
create table if not exists integration_train_requalifications (
    id bigserial primary key,
    project text not null check (project <> ''),
    -- Full canon commit ids: the train only qualifies full hashes (40 or 64
    -- hexadecimal characters, as `train_assembly._canon_commit` reads them).
    pin text not null check (pin ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
    gate text not null check (gate ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
    reviews jsonb not null check (jsonb_typeof(reviews) = 'array'
                                  and jsonb_array_length(reviews) > 0),
    lot bigint not null references work_items(id) on delete cascade,
    requester text not null check (requester <> ''),
    requested_at timestamptz not null default clock_timestamp(),
    state text not null default 'requested'
        check (state in ('requested', 'applied', 'refused')),
    resolved_at timestamptz,
    resolved_by text,
    error text,
    check ((state = 'requested') = (resolved_at is null))
);

-- One pending request per exact reading and evidence set: a repeated command
-- (lost response, retry) returns the same row instead of stacking duplicates.
-- The partial index also bounds the holder's scan to what it can still apply.
create unique index if not exists integration_train_requalifications_pending
    on integration_train_requalifications(project, pin, gate, reviews)
    where state = 'requested';

create index if not exists integration_train_requalifications_project
    on integration_train_requalifications(project, state, id);
