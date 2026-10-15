-- SPDX-License-Identifier: AGPL-3.0-only
-- 0443_account_daily_budget — shared daily USD envelope per provider billing
-- account (lot #1469, issue #1101; required for 1.11.0, TPK).
--
-- A billing account (`openai-manaty`, `deepseek-manaty`) is a logical id, the
-- same on every host: the host profiles that spend from it name it
-- (`accounts.<harness>[].billing_account`), whatever their key, name, agent
-- or host. Rotating a key, recreating an agent or moving a host therefore keeps
-- the same history and the same envelope.
--
-- `account_budgets`: one envelope per billing account, per CALENDAR day in
-- `tz` (Europe/Paris by default; 23 h or 25 h on DST days, same cap). Set by
-- a human only (`ameesh budget set --account`), journaled in `budget_events`
-- (`scope = 'account:<id>'`, `window_s = 0`).
--
-- `account_reservations`: before a turn on a billing account, the runner
-- reserves its bound under an advisory lock on the account, in one
-- transaction: settled spend of the day + open reservations + kept
-- reservations of the day + the new one must fit the cap. At the end of the
-- turn the reservation is `settled` (the real cost is in `turn_costs`, tagged
-- with `billing_account`), or `kept` when the outcome is uncertain (error,
-- crash, lost lease): it then keeps counting in full for its day until a human
-- releases it (`released`). At most one open reservation per agent.

create table if not exists account_budgets (
    billing_account text primary key
        check (billing_account ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    usd_per_day     double precision not null check (usd_per_day > 0),
    tz              text not null default 'Europe/Paris',
    set_by          text not null,
    updated_at      timestamptz not null default now()
);

create table if not exists account_reservations (
    id              bigserial primary key,
    billing_account text not null,
    agent           text not null,
    usd             double precision not null check (usd >= 0),
    period          date not null,
    state           text not null default 'open'
        check (state in ('open', 'settled', 'kept', 'released')),
    created_at      timestamptz not null default now(),
    closed_at       timestamptz,
    closed_by       text
);

create unique index if not exists account_reservations_open_agent
    on account_reservations (agent) where state = 'open';
create index if not exists account_reservations_account
    on account_reservations (billing_account, state, period);

alter table turn_costs add column if not exists billing_account text;
create index if not exists turn_costs_billing_account
    on turn_costs (billing_account, recorded_at) where billing_account is not null;

-- Same wake-up as `budget_limits` (0042): runners drop their cache and the
-- paused agents retry.
create or replace function ameesh_account_budget_notify() returns trigger
language plpgsql as $$
begin
    perform pg_notify('ameesh_budget', json_build_object(
        'scope', 'account:' || coalesce(new.billing_account, old.billing_account),
        'window_s', 0)::text);
    return null;
end $$;

drop trigger if exists account_budgets_notify on account_budgets;
create trigger account_budgets_notify
    after insert or update or delete on account_budgets
    for each row execute function ameesh_account_budget_notify();
