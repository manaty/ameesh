-- 0005_key_roles — deux rôles de clés, parce qu'une clé d'agent n'est pas
-- l'autorité du propriétaire (R4).
--
--   role 'owner' : la clé privée n'est détenue que par le propriétaire ;
--                  elle seule porte l'autorité (messages et approbations) ;
--   role 'agent' : la clé authentifie un agent (provenance, intégrité,
--                  échéance) mais ne confère AUCUNE autorité propriétaire et
--                  ne peut pas approuver.
--
-- Le défaut est 'agent' (moindre privilège) : enregistrer une clé propriétaire
-- est un acte explicite (`agent-mesh key register … --role owner`), jamais un
-- effet de bord.

alter table agent_registry
    add column if not exists key_role text not null default 'agent'
    check (key_role in ('owner', 'agent'));

-- La vue d'observabilité figeait ses colonnes (0003) : on la recrée pour que
-- `key_role` apparaisse, et on expose `has_owner_key` pour `mesh list`.
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
    (select count(*) from agent_mailbox m
      where m.recipient = r.name and m.delivered_at is null) as unread
from agent_registry r;

-- Une approbation ne vaut que si la clé est propriétaire : le SQL le dit, en
-- plus du contrôle Python, pour que la vue ne laisse passer aucun doute.
-- (CREATE OR REPLACE n'ajoute des colonnes qu'à la fin : `key_is_owner` vient
-- après `expires_ts`, l'ordre existant ne bouge pas.)
create or replace view mesh_approvals_status as
select
    a.*,
    (a.consumed_at is null)                    as unconsumed,
    (a.expires_at > now())                     as unexpired,
    (r.public_key is not null)                 as key_known,
    (r.key_revoked_at is null)                 as key_live,
    (r.public_key_fingerprint = a.signature_key) as key_matches,
    extract(epoch from a.expires_at)::float8   as expires_ts,
    (r.key_role = 'owner')                     as key_is_owner
from mesh_approvals a
left join agent_registry r on r.name = a.approver;
