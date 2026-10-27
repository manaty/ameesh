-- SPDX-License-Identifier: AGPL-3.0-only
-- #3922: index for loose group seeks and latest production lookup.
-- NULL and empty accounts remain separate; all tuple components are non-null.
-- Additive only: retain existing indexes for history and publication paths.
create index if not exists quota_gauge_readings_seek_idx
    on quota_gauge_readings
       (harness, (account is null), (coalesce(account, '')), gauge_key,
        produced_at desc nulls last, observed_at desc, id desc);

-- #3755: each publisher must keep its independent overage state. Source has
-- the same NULL/empty distinction as account; putting it before production
-- lets per-source reads jump over history too. The source-free index above
-- remains necessary for cross-source latest production order.
create index if not exists quota_gauge_readings_source_seek_idx
    on quota_gauge_readings
       (harness, (account is null), (coalesce(account, '')), gauge_key,
        (source is null), (coalesce(source, '')),
        produced_at desc nulls last, observed_at desc, id desc);
