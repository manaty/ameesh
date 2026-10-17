-- SPDX-License-Identifier: AGPL-3.0-only
-- #2229 : la vue d'observabilité expose `runner_seen_*` (0411) sur TOUTE base.
--
-- 0428 a recréé `agent_mesh_overview` avec `r.*`, que Postgres développe et
-- fige à la création. 0411 (numéro inférieur, fusionnée après) ajoute
-- `runner_seen_at/host/owner` à `agent_registry` : sur une base neuve elle
-- passe avant 0428 et la vue les a ; sur une base où 0428 était déjà
-- appliquée, la vue ne les a pas, et le contrat du superviseur
-- (deploy/sql/role-superviseur.sql) refuse l'installation.
--
-- La vue est recréée avec une liste de colonnes EXPLICITE : exactement celle
-- d'une base neuve (mêmes colonnes, même ordre), donc aucun droit nouveau à
-- classer. Une colonne future d'`agent_registry` n'y entre que par une
-- migration qui recrée la vue (garde : tests/test_migrations_vues.py).
-- Aucun objet ne dépend de la vue ; ses droits (accordés par les scripts de
-- rôle) sont relevés avant la suppression et rendus à l'identique.
create temp table m0460_acl on commit drop as
select a.attname::text as col, x.grantee, x.privilege_type, x.is_grantable
  from pg_attribute a, aclexplode(a.attacl) x
 where a.attrelid = to_regclass('agent_mesh_overview')
   and a.attnum > 0 and not a.attisdropped
union all
select null, x.grantee, x.privilege_type, x.is_grantable
  from pg_class c, aclexplode(c.relacl) x
 where c.oid = to_regclass('agent_mesh_overview');

drop view if exists agent_mesh_overview;
create view agent_mesh_overview as
select
    r.name, r.chantier, r.harness, r.host, r.cwd, r.session_id, r.status,
    r.status_text, r.model, r.budget_usd, r.spent_usd, r.turns,
    r.pending_prompt, r.last_error, r.lease_owner, r.lease_expires_at,
    r.lease_epoch, r.last_turn_at, r.last_seen, r.created_at, r.updated_at,
    r.public_key, r.public_key_fingerprint, r.key_updated_at, r.key_revoked_at,
    r.key_role, r.current_prompt, r.responsible, r.team, r.provider,
    r.credential_mode, r.ephemeral, r.ephemeral_expires_at, r.created_by,
    r.canon_ref, r.capabilities, r.last_event_at, r.placement_ok,
    r.placement_diagnostic, r.placement_ref, r.placement_profile,
    r.session_policy, r.effort, r.tier, r.session_work_item, r.status_since,
    r.restart_brief, r.restart_requested_at, r.session_reset_at, r.priority,
    r.admitted_hosts, r.admitted_tags, r.memory_repository, r.visibility_ok,
    r.visibility_diagnostic, r.mode, r.stop_reason, r.canon, r.session_account,
    r.context_max_tokens, r.turn_max_seconds, r.turn_mail_max,
    r.turn_grace_seconds, r.handoff_fenced, r.lot_wake, r.session_harness,
    r.session_cwd, r.session_origin, r.projects, r.lease_wait_since,
    r.workstream, r.runner_seen_at, r.runner_seen_host, r.runner_seen_owner,
    r.roles,
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

-- Les droits d'avant, rendus à l'identique (le propriétaire garde les siens
-- de droit). Les colonnes nouvelles pour cette base (`runner_seen_*`) restent
-- fermées jusqu'au prochain passage des scripts de rôle, qui les accordent.
do $$
declare
    g record;
begin
    for g in
        select a.col, a.privilege_type, a.is_grantable,
               case when a.grantee = 0 then 'public'
                    else quote_ident(pg_get_userbyid(a.grantee)) end as grantee
          from pg_temp.m0460_acl a
         where a.grantee <> (select relowner from pg_class
                              where oid = 'agent_mesh_overview'::regclass)
    loop
        execute format('grant %s%s on agent_mesh_overview to %s%s',
                       g.privilege_type,
                       case when g.col is null then '' else format(' (%I)', g.col) end,
                       g.grantee,
                       case when g.is_grantable then ' with grant option' else '' end);
    end loop;
end $$;
