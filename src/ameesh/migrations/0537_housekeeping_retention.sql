-- SPDX-License-Identifier: AGPL-3.0-only
-- #5588 : index réservé à la purge, sans détourner les lectures à fenêtre
-- sur at (#5411). La conversion UTC est immuable et conserve les instants.
CREATE INDEX IF NOT EXISTS housekeeping_log_retention_utc_idx
    ON housekeeping_log ((at AT TIME ZONE 'UTC'));
