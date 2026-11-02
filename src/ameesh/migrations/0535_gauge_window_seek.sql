-- SPDX-License-Identifier: AGPL-3.0-only
-- #5542: seek each gauge by window expiry, not by the account's whole history.
--
-- #5466 orders the readings of a gauge by `resets_at` first (a later expiry
-- proves the previous window is closed), then by production and insertion.
-- The #0493 seek index is ordered by `produced_at`; it cannot serve that
-- `ORDER BY`, so the query fell back to scanning and sorting every reading of
-- the account (19 s per call on the central, 35 concurrent calls).
--
-- Additive only: the existing indexes stay for the production-first reads.
create index if not exists quota_gauge_readings_window_seek_idx
    on quota_gauge_readings
       (harness, (account is null), (coalesce(account, '')), gauge_key,
        resets_at desc nulls last, produced_at desc nulls last,
        observed_at desc, id desc);
