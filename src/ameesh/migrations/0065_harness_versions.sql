-- SPDX-License-Identifier: AGPL-3.0-only
-- 0065_harness_versions — harness versions per host (lot L120, #119).
--
-- ameesh never updates the harnesses: it runs the `claude`, `codex`, `dsh`
-- it finds. Each runner now reads the version of the harnesses it serves
-- (`<binary> --version`) at start and every 6 h; `ameesh harness versions`
-- shows the differences between hosts and with the latest release. One row
-- per (host, harness): the last reading. `version` is NULL when the binary
-- answered nothing readable (`raw` keeps its first line).

create table if not exists harness_versions (
    host        text not null,
    harness     text not null,
    path        text,
    version     text,
    raw         text,
    observed_at timestamptz not null default now(),
    primary key (host, harness)
);
