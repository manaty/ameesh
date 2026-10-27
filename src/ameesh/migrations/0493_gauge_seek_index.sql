-- SPDX-License-Identifier: AGPL-3.0-only
-- #3922: index for loose group seeks and latest production lookup.
-- NULL and empty accounts remain separate; all tuple components are non-null.
-- Additive only: retain existing indexes for history and publication paths.
create index if not exists quota_gauge_readings_seek_idx
    on quota_gauge_readings
       (harness, (account is null), (coalesce(account, '')), gauge_key,
        produced_at desc nulls last, observed_at desc, id desc);
