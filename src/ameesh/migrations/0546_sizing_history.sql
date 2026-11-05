-- SPDX-License-Identifier: AGPL-3.0-only
-- #5921: nullable for N-1 writers and hosts without cgroup v2 counters.
alter table turn_resources add column if not exists memory_peak_bytes bigint;
alter table turn_resources add column if not exists cpu_usage_usec bigint;

-- UTC calendar days. Host samples and turn peaks have separate populations.
create table if not exists host_resource_daily (
    host text not null,
    day date not null,
    host_metrics jsonb,
    turn_metrics jsonb,
    primary key (host, day)
);
create index if not exists host_resource_daily_day_idx on host_resource_daily(day);
create index if not exists turn_resources_sizing_idx
    on turn_resources(host, started_at) where memory_peak_bytes is not null or cpu_usage_usec is not null;
