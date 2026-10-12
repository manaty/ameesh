-- SPDX-License-Identifier: AGPL-3.0-only
-- 0361_guard_restart — the guard's last restart in host resource readings
-- (L159e6, #301).
--
-- After a stop, the capacity guard of a lending host starts again, once, the
-- agent units it stopped itself (L159e4); it publishes the outcome in its
-- local state (`guard.restart`: {ts, started, skipped: {unit: reason}}),
-- which only the host could read. The runner of the host copies it into its
-- readings, so `ameesh alerts` raises `guard_restart`. Null when the host has
-- no guard, its state is unreadable or it never restarted anything.
-- The supervisor role reads it (deploy/sql/role-superviseur.sql).

alter table host_resources add column if not exists guard_restart jsonb;
