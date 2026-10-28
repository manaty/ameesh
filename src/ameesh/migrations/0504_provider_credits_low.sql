-- SPDX-License-Identifier: AGPL-3.0-only
-- 0504_provider_credits_low — declared prepaid amount and assessed balance
-- (#4166, suite de #4164 / manaty/ameesh#3181).
--
-- 0500 holds the real credit state of each provider account (ok | low |
-- exhausted). This migration adds what the `low` state is computed from:
--
-- * `prepaid_usd`, `prepaid_at`, `prepaid_by`: the owner declares the amount
--   available on an account that exposes no balance API (OpenAI, Codex). The
--   balance is then PROJECTED: `prepaid_usd` minus the settled spend
--   (`turn_costs`) of that (harness, account) since `prepaid_at`. Declaring
--   again states the current amount (a top-up) and starts a new projection.
--   The declaration is human-only, like a budget cap (`ameesh budget
--   prepaid set`).
-- * `balance_usd`, `ratio`, `runway_at`: the last assessment — the projected
--   balance (or the real balance read from the provider, DeepSeek), its share
--   of the prepaid/reference amount (0..1), and the instant the account is
--   projected to run out at the measured spend rate. They are a snapshot for
--   display and alerts, never a secret nor a prompt.
--
-- Additive only; a row without a declaration behaves exactly as in 0500 (the
-- state is driven by failures, a zero balance reading, and the probe).
alter table provider_account_credits
    add column if not exists prepaid_usd numeric(14, 6),
    add column if not exists prepaid_at timestamptz,
    add column if not exists prepaid_by text,
    add column if not exists balance_usd numeric(14, 6),
    add column if not exists ratio numeric(7, 6),
    add column if not exists runway_at timestamptz;

comment on column provider_account_credits.prepaid_usd is
    '#4166: amount declared by a human for an account without a balance API; '
    'the balance is projected from the settled spend since prepaid_at';
comment on column provider_account_credits.balance_usd is
    '#4166: last assessed balance (projected or read) — display and alerts';
comment on column provider_account_credits.ratio is
    '#4166: assessed balance / reference amount (prepaid, or the peak of the '
    'balance series); low at 20 %, critical at 5 %';
comment on column provider_account_credits.runway_at is
    '#4166: projected instant the account runs out at the measured spend rate';
