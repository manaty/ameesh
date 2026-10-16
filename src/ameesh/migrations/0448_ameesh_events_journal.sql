-- SPDX-License-Identifier: AGPL-3.0-only
-- 0448_ameesh_events_journal — a random identity per event journal (L85, lot
-- #928, R2 auditeur de #373, F5).
--
-- `tx` only orders the events of ONE journal: `ameesh_events` lives in a
-- schema (or a base), and two meshes of the same cluster — two schemas, two
-- bases — each have their own log. A position (Last-Event-ID) must therefore
-- name the journal itself, not only the cluster and its timeline: after a
-- change of AMEESH_SCHEMA, the position of mesh A used to compare equal to
-- the identity of mesh B, and B skipped its own events without a reset
-- (witness W5). The identifier is drawn once, here, and kept in the
-- journal's singleton row; a reader that may not read it gets the identity
-- refused (NULL) and every resume is a reset — never a silent skip.
--
-- Additive: it does not touch 0396's table, and re-defines its identity
-- function with the same signature and the same fallbacks.

create table if not exists ameesh_events_journal (
    one     boolean primary key default true check (one),
    -- 16 hex digits: drawn once, short enough for an SSE `id:` line
    journal text not null default substr(replace(gen_random_uuid()::text, '-', ''), 1, 16)
);
insert into ameesh_events_journal (one) values (true) on conflict (one) do nothing;

-- The identity a position carries: "<system_identifier>t<timeline>j<journal>".
-- Same exceptions as 0396: in recovery the timeline comes from the last
-- checkpoint, and a role that may not read the journal's identity gets NULL.
create or replace function ameesh_events_cluster() returns text
language plpgsql stable as $$
declare
    timeline text;
begin
    begin
        timeline := substr(pg_walfile_name(pg_current_wal_lsn()), 1, 8);
    exception when object_not_in_prerequisite_state then   -- in recovery
        timeline := lpad(to_hex((select timeline_id from pg_control_checkpoint())), 8, '0');
    end;
    return (select to_hex(system_identifier) from pg_control_system()) || 't'
        || lower(timeline) || 'j'
        || (select journal from ameesh_events_journal where one);
exception when insufficient_privilege then
    return null;
end $$;
