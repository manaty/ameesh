-- SPDX-License-Identifier: AGPL-3.0-only
-- 0428_overview_projects — the observability view exposes `projects` (L243-3,
-- #810). `ameesh list` reads the agent's primary project and shows `ameesh+1`
-- when it declares others.
--
-- La vue d'observabilité fige ses colonnes (`r.*` développé à la création) :
-- recréée à l'identique de 0033, donc augmentée de tout ce que
-- `agent_registry` a gagné depuis — `projects` (0388) et `workstream` (0410).
drop view if exists agent_mesh_overview;
create view agent_mesh_overview as
select
    r.*,
    extract(epoch from r.lease_expires_at)::float8 as lease_expires_ts,
    extract(epoch from r.last_seen)::float8        as last_seen_ts,
    extract(epoch from r.last_turn_at)::float8     as last_turn_ts,
    (r.public_key_fingerprint is not null and r.key_revoked_at is null) as key_ready,
    (r.public_key_fingerprint is not null and r.key_revoked_at is null
     and r.key_role = 'owner')                                        as has_owner_key,
    extract(epoch from r.ephemeral_expires_at)::float8                as ephemeral_expires_ts,
    (coalesce(r.responsible, '') <> ''
     and (not r.ephemeral or r.ephemeral_expires_at > now()))         as responsible_ok,
    (select count(*) from agent_mailbox m
      where m.recipient = r.name and m.delivered_at is null) as unread
from agent_registry r;
