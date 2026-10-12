-- SPDX-License-Identifier: AGPL-3.0-only
-- 0392_host_holds — the owner holds a host's runners on purpose (lot #254).
--
-- Stopping a host's runners on purpose (a disk migration, a night of load)
-- used to look like an outage: dead runners, stale lots, idle capacity, each
-- one advising a gesture against the hold. A hold is recorded here, one per
-- host, by `ameesh hosts HOST --hold --until … --reason …`, and lifted by
-- `--release`. It always ends: `until` is required, after the moment the hold
-- is set and 24 h at most past it, both on database time (the CHECK bounds
-- every write, whatever the caller's clock), and past it the hold is ignored
-- without any cleanup. While it
-- lasts, `ameesh alerts` mutes the nudges about the agents admitted only on
-- held hosts and says so in one `host_hold` line per host.

create table if not exists host_holds (
    host       text primary key check (host <> ''),
    reason     text not null check (reason <> ''),
    author     text not null check (author <> ''),
    created_at timestamptz not null default now(),
    until      timestamptz not null,
    check (until > created_at and until <= created_at + interval '24 hours')
);

-- Every hold and every release, in the same transaction as the change: a
-- replacement overwrites the hold and a release deletes it, the history
-- stays (the project thread only keeps a readable copy).
create table if not exists host_hold_events (
    id         bigserial primary key,
    host       text not null check (host <> ''),
    action     text not null check (action in ('hold', 'release')),
    author     text not null check (author <> ''),
    reason     text,
    until      timestamptz,
    at         timestamptz not null default now(),
    check ((action = 'hold') = (reason is not null and until is not null))
);
create index if not exists host_hold_events_host on host_hold_events (host, at);
