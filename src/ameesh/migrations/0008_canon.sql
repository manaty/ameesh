-- 0008_canon — le canon OKF dans le registre (C2, C3 ; spec §4.4).
--
-- Colonnes DÉCLARATIVES : écrites par `ameesh canon sync` depuis les fiches
-- Agent/Placement lues à la révision canonique, ou par `ameesh agent spawn`
-- pour un agent éphémère. Elles ne remplacent aucune colonne d'état (bail,
-- session, statut, dépense, consigne), que sync ne touche jamais.
--
--   responsible          humain responsable RÉSOLU (`human:<id>`) ; NULL tant
--                        que la fiche ne résout pas vers un Member humain ou
--                        que le canon a une erreur bloquante pour cet agent
--   team                 équipe ou projet de la fiche
--   provider             fournisseur facturant le modèle
--   credential_mode      api-key | subscription (placement, sinon fiche)
--   ephemeral            agent sans fiche, créé par un autre agent (R14)
--   ephemeral_expires_at échéance obligatoire d'un éphémère
--   created_by           agent créateur d'un éphémère
--   canon_ref            `<membre>:<chemin>@<commit>` de la fiche lue
--   capabilities         capacités déclarées ; `approve` interdit (R8)

alter table agent_registry
    add column if not exists responsible          text,
    add column if not exists team                 text,
    add column if not exists provider             text,
    add column if not exists credential_mode      text,
    add column if not exists ephemeral            boolean not null default false,
    add column if not exists ephemeral_expires_at timestamptz,
    add column if not exists created_by           text,
    add column if not exists canon_ref            text,
    add column if not exists capabilities         text[];

-- Un éphémère a toujours une échéance (spec §4.4).
alter table agent_registry
    add constraint agent_registry_ephemeral_expiry_chk
    check (not ephemeral or ephemeral_expires_at is not null);

-- R8 : `approve` n'est jamais une capacité d'agent, même écrite à la main en
-- base. Le contrôle Python (validation du canon) reste la première barrière.
alter table agent_registry
    add constraint agent_registry_no_approve_chk
    check (capabilities is null
           or lower(array_to_string(capabilities, ',')) !~ '(^|,)\s*approve\s*(,|$)');

-- La vue d'observabilité figeait ses colonnes (0005) : on la recrée pour que
-- les colonnes du canon apparaissent dans `mesh list --json`, avec
-- `responsible_ok` (R14 : l'agent a un responsable et, s'il est éphémère,
-- n'est pas échu).
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
