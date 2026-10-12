-- SPDX-License-Identifier: AGPL-3.0-only
-- 0362_agent_watches — the watches ameesh manages (L121, lot #120).
--
-- Before L121 the watch of an orchestrator was a systemd user unit written by
-- hand on one workstation, its instruction text in the unit itself: neither
-- versioned nor visible. `ameesh watch enable` now installs the timer on the
-- host it runs on (each firing runs `ameesh watch send`, which sends the
-- instruction shipped with the package, `consignes/veille.md`) and records
-- the watch here, so `ameesh projects` shows it from any host.
--
-- `agent_watches`: one row per watched agent (a watch moves with the last
-- `enable`); `disable` deletes it. `last_sent_at`: the last instruction sent.

create table if not exists agent_watches (
    agent        text primary key,
    every_s      integer not null check (every_s >= 60),
    host         text not null,
    enabled_by   text not null,
    enabled_at   timestamptz not null default now(),
    last_sent_at timestamptz
);
