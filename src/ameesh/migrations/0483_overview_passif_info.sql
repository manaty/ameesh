-- SPDX-License-Identifier: AGPL-3.0-only
-- #3451 : nouvelle nature passive `info` (avis L165 aux pilotes, compte rendu
-- sans geste attendu, `agent-mail send --info`) : lue au prochain tour, sans
-- en ouvrir un. `passive_unread` la compte comme les autres passifs, avec le
-- même prédicat que `storage.postgres.mailbox.PASSIVE_SQL` et
-- `mail.passive_reason`. Vue seule, liste de colonnes inchangée (0468) :
-- `create or replace` garde l'OID et les droits de colonne.
create or replace view agent_mesh_overview as
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
    -- `unread` reste tous les non-lus (contrat de la liste humaine) ; le
    -- passif est compté à part, avec le même prédicat que
    -- `storage.postgres.mailbox.PASSIVE_SQL` et `mail.passive_reason`
    -- (L125) — un urgent perce toujours.
    (select count(*) from agent_mailbox m
      where m.recipient = r.name and m.delivered_at is null) as unread,
    (select count(*) from agent_mailbox m
      where m.recipient = r.name and m.delivered_at is null
        and (NOT m.payload @> '{"urgent": true}'::jsonb
             AND (m.payload @> '{"ack": true}'::jsonb
              OR m.payload @> '{"broadcast": true}'::jsonb
              OR m.payload @> '{"ci_passif": true}'::jsonb
              OR m.payload @> '{"info": true}'::jsonb
              OR coalesce(m.payload->>'cc', '') <> ''
              OR coalesce(jsonb_typeof(m.payload->'radar'), '') = 'object'))) as passive_unread
from agent_registry r;
