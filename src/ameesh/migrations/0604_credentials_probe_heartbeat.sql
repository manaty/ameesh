-- SPDX-License-Identifier: AGPL-3.0-only
-- 0604_credentials_probe_heartbeat — #8062: the sentinel probe's own date.
--
-- `checked_at` is written by every report, including a runner's `turn`
-- failure (L226b): a repeated turn failure must not mask a stopped probe.
-- `probed_at` only advances when the sentinel reads the credential (a
-- `source = 'sentinel'` report, or the kept-failure touch). Existing
-- sentinel rows are backfilled from their last reading so the rollout does
-- not raise a false "never probed" alert; a turn-only row stays NULL and is
-- named as such.
alter table credential_health add column if not exists probed_at timestamptz;

update credential_health set probed_at = checked_at
 where probed_at is null and source = 'sentinel';
