-- SPDX-License-Identifier: AGPL-3.0-only
-- #5561 / #4162: exact-marker incident and human resolution journal.
create table if not exists spend_marker_events (
    id bigserial primary key,
    agent text not null,
    marker_created_at timestamptz not null,
    marker_spend_key text,
    event text not null check (event in ('ambiguous', 'link', 'abandon')),
    ledger_key text,
    actor text not null,
    reason text not null check (length(trim(reason)) > 0),
    snapshot jsonb not null,
    created_at timestamptz not null default clock_timestamp()
);
create unique index if not exists spend_marker_incident_once
    on spend_marker_events (agent, marker_created_at)
    where event = 'ambiguous';
create index if not exists spend_marker_resolution
    on spend_marker_events (agent, marker_created_at, id desc)
    where event = 'link';

create trigger ameesh_agent_spelling before insert or update of agent on spend_marker_events
    for each row execute function ameesh_agent_spelling('agent');
