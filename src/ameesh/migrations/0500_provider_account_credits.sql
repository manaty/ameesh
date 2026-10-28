-- SPDX-License-Identifier: AGPL-3.0-only
-- 0500_provider_account_credits — real credit state of each provider account
-- (#4164, manaty/ameesh#3181, OpenAI IMA incident of 2026-10-28).
--
-- ameesh budgets are spend ENVELOPES: they never saw that the provider
-- account itself had run out of prepaid credits (`429 insufficient_quota`),
-- so turns kept being granted and failing. This table holds the provider
-- account's own state, keyed by the LOGICAL account (`harness`, `account`
-- name as declared on every host; '' = the harness default), never by host:
-- the prepaid balance is the provider's, shared by every runner.
--
-- * `state`: ok | low | exhausted. A turn failing with a credit-exhaustion
--   signature (classified per provider by `ameesh.provider_credits`), or a
--   balance reading at zero, sets `exhausted`; a successful probe turn or a
--   positive balance reading sets `ok`.
-- * `since_ts`: start of the current state (alert key: one alert per
--   transition, one recovery notice when the row leaves `exhausted`).
-- * `checked_at`: last check (failure, probe, balance reading).
-- * `next_probe_at`: while exhausted, the earliest instant one turn of the
--   account is let through as a bounded probe (claimed atomically).
--
-- Additive only. No secret, no prompt: `reason` is a short classification,
-- `detail` the provider error text truncated by the caller.
create table if not exists provider_account_credits (
    harness        text not null,
    account        text not null default '',
    state          text not null default 'ok'
        check (state in ('ok', 'low', 'exhausted')),
    since          timestamptz not null default now(),
    checked_at     timestamptz not null default now(),
    next_probe_at  timestamptz,
    reason         text,
    detail         text,
    source         text,
    agent          text,
    host           text,
    primary key (harness, account)
);

comment on table provider_account_credits is
    '#4164: provider account credit state (ok|low|exhausted), shared by all '
    'runners; exhausted accounts get no new turns except one bounded probe.';
