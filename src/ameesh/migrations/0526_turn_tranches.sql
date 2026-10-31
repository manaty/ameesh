-- SPDX-License-Identifier: AGPL-3.0-only
-- #4872 / #2202 S3. No real tranche or budget is created here.
create table tranche_limits (
    id text primary key,
    project text not null,
    cap_usd numeric(12,2) not null check (cap_usd > 0 and cap_usd <> 'NaN'),
    currency text not null default 'USD' check (currency = 'USD'),
    tz text not null default 'Europe/Paris',
    starts_at timestamptz not null,
    ends_at timestamptz check (ends_at > starts_at),
    state text not null default 'open' check (state in ('open','closed')),
    overdraft boolean not null default false,
    admitted_by text not null,
    admitted_ref text not null,
    created_at timestamptz not null default now()
);
create table tranche_reservations (
    id bigserial primary key,
    tranche_id text not null references tranche_limits(id),
    spend_key text not null unique references account_reservations(spend_key),
    turn_id text not null unique,
    agent text not null,
    billing_account text not null,
    scope_digest text,
    path text not null,
    reserved_usd numeric(12,6) not null check (reserved_usd > 0 and reserved_usd <> 'NaN'),
    usd numeric(12,6) not null check (usd >= 0 and usd <> 'NaN'),
    state text not null default 'open' check (state in ('open','settled','kept','released')),
    created_at timestamptz not null default now(),
    closed_at timestamptz,
    closed_by text
);
create index tranche_reservations_usage on tranche_reservations(tranche_id,state);
create table tranche_events (
    id bigserial primary key,
    tranche_id text not null references tranche_limits(id),
    reservation_id bigint references tranche_reservations(id),
    action text not null,
    actor text not null,
    reason text not null,
    at timestamptz not null default now(),
    data jsonb not null default '{}'
);
-- A measured row is not a complete measurement while another paid request
-- is still in flight (or its relay crashed before recording the cost).
create table tranche_relay_requests (
    id text primary key,
    turn_id text not null references sandbox_turns(turn_id),
    state text not null default 'open' check (state in ('open','recorded')),
    created_at timestamptz not null default now(),
    recorded_at timestamptz
);
create index tranche_relay_requests_pending on tranche_relay_requests(turn_id) where state = 'open';

create trigger ameesh_agent_spelling before insert or update of agent,closed_by on tranche_reservations
    for each row execute function ameesh_agent_spelling('agent closed_by');
create trigger ameesh_agent_spelling before insert or update of actor on tranche_events
    for each row execute function ameesh_agent_spelling('actor');
create trigger ameesh_agent_spelling before insert or update of admitted_by on tranche_limits
    for each row execute function ameesh_agent_spelling('admitted_by');
