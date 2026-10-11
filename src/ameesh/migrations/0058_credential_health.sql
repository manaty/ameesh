-- SPDX-License-Identifier: AGPL-3.0-only
-- 0058_credential_health — health of each harness credential per host
-- (lots L226a and L226b).
--
-- One row per (host, harness, account): the last known health of the
-- credential the harness uses for that account. `account` is the L30 account
-- name, '' for the harness's default credential (no accounts declared).
--
-- Two writers:
-- * the sentinel (L226a, `source = 'sentinel'`) probes each credential and
--   reports `healthy` or `failing`;
-- * the runner (L226b, `source = 'turn'`) reports `failing` when a turn fails
--   for an authentication reason.
--
-- A `failing` credential is never used for a turn: the account failover
-- (L30 `choose`) skips it, and an agent with no healthy account left stays
-- `auth_blocked` (status `blocked`, `status_text` starting with
-- `auth_blocked:`) until the credential is reported `healthy` again.

create table if not exists credential_health (
    host          text not null,
    harness       text not null,
    account       text not null default '',
    state         text not null check (state in ('healthy', 'failing', 'unknown')),
    reason        text not null default '',
    source        text not null check (source in ('sentinel', 'turn', 'manual')),
    checked_at    timestamptz not null default now(),
    -- start of the current failure (kept while `failing` is reported again,
    -- cleared on `healthy`)
    failing_since timestamptz,
    primary key (host, harness, account)
);
