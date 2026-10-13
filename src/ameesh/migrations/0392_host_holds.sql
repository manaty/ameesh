-- SPDX-License-Identifier: AGPL-3.0-only
-- 0392_host_holds — the owner holds a host's runners on purpose (lot #254).
--
-- Stopping a host's runners on purpose (a disk migration, a night of load)
-- used to look like an outage: dead runners, stale lots, idle capacity, each
-- one advising a gesture against the hold. A hold is recorded here, one per
-- host, by `ameesh hosts HOST --hold --until … --reason …`, and lifted by
-- `--release`. It always ends: `until` is required (24 h at most, checked by
-- the CLI), and past it the hold is ignored without any cleanup. While it
-- lasts, `ameesh alerts` mutes the nudges about the agents admitted only on
-- held hosts and says so in one `host_hold` line per host.

create table if not exists host_holds (
    host       text primary key check (host <> ''),
    reason     text not null check (reason <> ''),
    author     text not null check (author <> ''),
    created_at timestamptz not null default now(),
    until      timestamptz not null
);
