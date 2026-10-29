-- SPDX-License-Identifier: AGPL-3.0-only
-- #4256: validation routing snapshots and a private, factual audit trail.
set local lock_timeout = '5s';
create table decision_validations (
    decision_id bigint primary key references agent_mailbox(id),
    routing jsonb not null,
    escalated_at timestamptz
);
create table validation_events (
    id bigserial primary key,
    decision_id bigint not null references decision_validations(decision_id),
    person text not null,
    kind text not null,
    occurred_at timestamptz not null,
    facts jsonb not null,
    event_key text not null,
    unique (decision_id, event_key)
);
create table validation_decision_links (
    source_id bigint not null references decision_validations(decision_id),
    target_id bigint not null references agent_mailbox(id),
    relation text not null check (relation = 'contradicts'),
    response_key text not null,
    primary key (source_id, response_key)
);
create index validation_events_person_idx on validation_events(person, id);
