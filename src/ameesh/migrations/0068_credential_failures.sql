-- SPDX-License-Identifier: AGPL-3.0-only
-- 0068_credential_failures — the last failure of a credential (lot L226a,
-- #210), additive to 0060_credential_health (shipped by L226b, #197).
--
-- The LAST `failing` report, its credential's fingerprint (truncated SHA-256
-- of the refresh token or key, never the credential; null when unknown or
-- not failing) and the count of `failing` reports: the sentinel clears a
-- failure only for another credential than the last failure's, and only if
-- no failure was counted since it read the row. The fingerprint is not
-- granted to the supervisor role (deploy/sql/role-superviseur.sql).

alter table credential_health add column if not exists last_failure_at timestamptz;
alter table credential_health add column if not exists failing_fingerprint text;
alter table credential_health add column if not exists failures integer not null default 0;
