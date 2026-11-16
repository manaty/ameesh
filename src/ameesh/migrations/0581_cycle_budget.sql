-- SPDX-License-Identifier: AGPL-3.0-only
-- #7518: expiring credit projection and per-attempt monetary reservations.
-- Integer micro-USD: ceilings for liabilities, floors for available credit.
create table if not exists credit_cycles (
    id bigserial primary key,
    billing_account text not null check (billing_account ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    credit_micro bigint not null check (credit_micro >= 0),
    cap_micro bigint not null check (cap_micro >= 0 and cap_micro <= credit_micro),
    observed_at timestamptz not null,
    expires_at timestamptz not null,
    fresh_until timestamptz not null,
    source text not null check (length(source) > 0),
    actor text not null,
    breached boolean not null default false,
    created_at timestamptz not null default clock_timestamp(),
    check (isfinite(observed_at) and isfinite(expires_at) and isfinite(fresh_until)),
    check (observed_at < fresh_until and fresh_until <= expires_at)
);
create index if not exists credit_cycles_account on credit_cycles (billing_account, id desc);

create table if not exists credit_requests (
    attempt_id text primary key check (length(attempt_id) between 1 and 200),
    cycle_id bigint not null references credit_cycles(id),
    billing_account text not null,
    agent text not null,
    bound_micro bigint not null check (bound_micro > 0),
    actual_micro bigint check (actual_micro >= 0),
    state text not null default 'open' check (state in ('open', 'settled', 'kept')),
    pricing jsonb not null,
    created_at timestamptz not null default clock_timestamp(),
    closed_at timestamptz,
    check (state <> 'settled' or actual_micro is not null)
);
create index if not exists credit_requests_cycle on credit_requests (cycle_id);
-- Uncertain attempts from previous cycles remain liabilities on this account.
create index if not exists credit_requests_pending on credit_requests (billing_account)
    where state in ('open', 'kept');
create index if not exists credit_requests_account_cycle
    on credit_requests (billing_account, cycle_id);
create index if not exists credit_requests_late_settled
    on credit_requests (billing_account, closed_at) where state = 'settled';
