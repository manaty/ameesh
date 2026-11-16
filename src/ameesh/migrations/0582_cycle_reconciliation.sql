-- SPDX-License-Identifier: AGPL-3.0-only
-- #7520: explicit billing lag and append-only operator reconciliation evidence.
alter table credit_cycles add column if not exists billing_lag_seconds bigint
    not null default 0;
create table if not exists credit_reconciliations (
    id bigserial primary key,
    billing_account text not null,
    attempt_id text references credit_requests(attempt_id),
    event text not null check (event in ('settle', 'clear_breach')),
    actor text not null check (length(trim(actor)) > 0),
    source text not null check (length(trim(source)) > 0),
    reason text not null check (length(trim(reason)) > 0),
    evidence jsonb not null,
    created_at timestamptz not null default clock_timestamp()
);
create index if not exists credit_reconciliations_account
    on credit_reconciliations (billing_account, id);
