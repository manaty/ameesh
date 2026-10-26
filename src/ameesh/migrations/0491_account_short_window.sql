-- SPDX-License-Identifier: AGPL-3.0-only
-- 0491_account_short_window — follow the provider's short window per plan
-- account (#3779).
--
-- Codex (ChatGPT plan) exposes only a weekly `codex` bucket in
-- `account/rateLimits/read` (`windowDurationMins = 10080`). The provider also
-- enforces an undocumented account-wide burst/concurrency bucket: several
-- sessions of the same account get HTTP 429 on the first request of a turn
-- while the weekly gauge is far from its ceiling (openai/codex#41702,
-- holon-run/holon#3369). No `retry-after` or short-window gauge reaches the
-- client.
--
-- `account_rate_events` is the shared journal of those refusals. It is keyed
-- by the LOGICAL account (`harness`, `account` name as declared on every host;
-- all hosts of the codex pool name it `primaire`), never by host: the bucket is
-- the provider's, shared by every runner of the account. The business module
-- `ameesh.account_window` derives a cooldown from it (base 4 min, raised with
-- the number of refusals in 15 min, capped at 15 min), a concurrency cap from
-- `agent_registry.status = 'running'` and a minimum gap between two turn
-- starts from `account_turn_starts`. `accounts list` renders the short window
-- read from all three.
--
-- The tables are append-only from the runners; no secret, no prompt, no token:
-- `detail` is the provider error text, truncated by the caller.
create table if not exists account_rate_events (
    id        bigserial primary key,
    harness   text not null,
    account   text not null,
    host      text not null default '',
    agent     text not null default '',
    kind      text not null default 'rate_limited'
        check (kind in ('rate_limited')),
    detail    text,
    at        timestamptz not null default now()
);

create index if not exists account_rate_events_window_idx
    on account_rate_events (harness, account, at desc);

-- `account_turn_starts` : every turn start of the logical account. The start
-- gap cannot be read from `agent_registry` alone (a finished turn changes
-- `status_since` to its end), and it must be shared by every host: five starts
-- in 90 s across sessions produced 429s without any concurrency.
create table if not exists account_turn_starts (
    id        bigserial primary key,
    harness   text not null,
    account   text not null,
    host      text not null default '',
    agent     text not null default '',
    at        timestamptz not null default now()
);

create index if not exists account_turn_starts_window_idx
    on account_turn_starts (harness, account, at desc);

-- Retention is handled by the housekeeping pass like other operational
-- journals; the module only reads the last 15 minutes.
comment on table account_rate_events is
    'Provider short-window refusals (429), shared by (harness, account); #3779';
comment on table account_turn_starts is
    'Turn starts per logical account, for the short-window start gap; #3779';
