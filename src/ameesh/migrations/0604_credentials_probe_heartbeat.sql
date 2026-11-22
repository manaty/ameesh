-- SPDX-License-Identifier: AGPL-3.0-only
-- 0604_credentials_probe_heartbeat — #8062: the sentinel probe's own date.
--
-- `checked_at` is written by every report, including a runner's `turn`
-- failure (L226b): a repeated turn failure must not mask a stopped probe.
-- `probed_at` only advances when a probe reads a non-failing credential: the
-- new sentinel writes it, and the trigger below stamps it from `checked_at`
-- for an N-1 sentinel that does not know the column. A `turn` write never
-- replaces it, so repeated turn failures leave the last real probe date
-- visible.
--
-- A failing row is different: an N-1 kept failure writes nothing (no report,
-- and `annotate` leaves `checked_at` untouched), and an N-1 failing report
-- has no probe date of its own. The trigger drops the inherited date on such
-- a report; the reader then reads a failing row through `probed_at` alone,
-- so an active N-1 kept failure is not mistaken for a stopped timer. The
-- row is already named by `credential_failing`; a new sentinel stamps its
-- own date on each pass (`touch`), and that date ages normally.
--
-- `probe_missing_since` anchors the other case: a non-failing pair with no
-- probe date at all (the timer never ran, or its last writing predates this
-- migration and a turn overwrote it). It is set once — migration time for
-- existing rows, the write's `checked_at` for a new row — and cleared by the
-- first probe stamp; turn writes never move it. Without it, the fresh
-- `checked_at` of repeated turn failures would hide a probe that never ran.
--
-- The trigger only fills the two new columns. Previous code neither names
-- nor reads them, so it stays compatible with an N-1 writer during the
-- deployment wave.
-- ameesh: trigger compatible with previous code
alter table credential_health add column if not exists probed_at timestamptz;
alter table credential_health add column if not exists probe_missing_since timestamptz
    default now();

create or replace function credential_health_probe_heartbeat() returns trigger
language plpgsql as $$
begin
    if new.source = 'sentinel' then
        if new.state = 'failing' then
            -- An N-1 failing report advances `checked_at` but has no probe
            -- date of its own, and an N-1 kept failure writes nothing at all.
            -- Drop the inherited date so neither is read as a stopped probe;
            -- a new sentinel writes `probed_at` explicitly and keeps it.
            if tg_op = 'UPDATE'
                    and new.probed_at is not distinct from old.probed_at then
                new.probed_at := null;
            end if;
        elsif new.probed_at is null or new.probed_at < new.checked_at then
            -- An N-1 sentinel does not know `probed_at`: its `checked_at` is
            -- the probe reading. The new sentinel writes both dates.
            new.probed_at := new.checked_at;
        end if;
    end if;
    if new.probed_at is not null then
        new.probe_missing_since := null;
    elsif tg_op = 'INSERT' or new.probe_missing_since is null then
        -- First write of a pair that has never probed: keep that instant so
        -- repeated turn writes cannot restart the grace window.
        new.probe_missing_since := new.checked_at;
    end if;
    return new;
end
$$;

drop trigger if exists credential_health_probe_heartbeat on credential_health;
create trigger credential_health_probe_heartbeat
before insert or update on credential_health
for each row execute function credential_health_probe_heartbeat();
