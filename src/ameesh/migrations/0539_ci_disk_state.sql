-- SPDX-License-Identifier: AGPL-3.0-only
-- #5630 : mesure via annotations du workflow de base, aucun DSN sur les VM CI.
set local lock_timeout = '5s';
create table if not exists ci_disk_state (
    repo text not null,
    host text not null,
    volume text not null,
    run_id bigint,
    sampled_at timestamptz,
    free_bytes bigint check (free_bytes >= 0),
    total_bytes bigint check (total_bytes > 0),
    seen_at timestamptz not null default now(),
    primary key (repo, host, volume)
);
