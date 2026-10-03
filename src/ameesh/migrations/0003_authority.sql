-- 0003_authority — l'autorité du propriétaire, prouvée et non déduite (R4).
--
-- Une signature Ed25519 ne vaut que si :
--   * la clé PUBLIQUE de l'expéditeur est enregistrée dans agent_registry (et
--     non révoquée) ; la clé privée ne touche jamais la base ;
--   * la signature couvre un payload canonique (contenu : expéditeur,
--     destinataire, texte, horodatage, nonce) ;
--   * l'échéance n'est pas dépassée.
--
-- Les approbations (porte de gouvernance, spec §5 point 3) vivent dans
-- mesh_approvals : une décision signée, liée à une action et à l'empreinte d'un
-- artefact (diff, migration, release), à échéance, et consommable une fois.

alter table agent_registry
    add column if not exists public_key              text,
    add column if not exists public_key_fingerprint  text,
    add column if not exists key_updated_at          timestamptz,
    add column if not exists key_revoked_at          timestamptz;

create index if not exists agent_registry_key_fp_idx
    on agent_registry (public_key_fingerprint)
    where public_key_fingerprint is not null;

-- Les colonnes signature/signature_key existaient dès 0001 : on ajoute ce qui
-- manque pour vérifier sans ambiguïté.
alter table agent_mailbox
    add column if not exists nonce                text,
    add column if not exists signed_payload       text,
    add column if not exists signature_expires_at timestamptz;

create index if not exists agent_mailbox_signed_idx
    on agent_mailbox (signature_key)
    where signature is not null;

create table if not exists mesh_approvals (
    id            bigserial primary key,
    approver      text not null,
    action        text not null,
    artifact_kind text not null default 'text',
    artifact_hash text not null,
    decision      text not null default 'approved'
                  check (decision in ('approved','rejected')),
    nonce         text not null,
    signed_payload text not null,
    signature     text not null,
    signature_key text not null,
    created_at    timestamptz not null default now(),
    expires_at    timestamptz not null,
    consumed_at   timestamptz,
    consumed_by   text,
    meta          jsonb not null default '{}'::jsonb
);

create index if not exists mesh_approvals_lookup_idx
    on mesh_approvals (action, artifact_hash, decision, consumed_at);
create index if not exists mesh_approvals_approver_idx
    on mesh_approvals (approver, created_at desc);

-- L'état « clé connue, non révoquée, pas encore consommée, pas expirée » est
-- calculable en SQL ; la validité cryptographique est vérifiée en Python
-- (Ed25519 n'existe pas dans Postgres).
create or replace view mesh_approvals_status as
select
    a.*,
    (a.consumed_at is null)                    as unconsumed,
    (a.expires_at > now())                     as unexpired,
    (r.public_key is not null)                 as key_known,
    (r.key_revoked_at is null)                 as key_live,
    (r.public_key_fingerprint = a.signature_key) as key_matches,
    extract(epoch from a.expires_at)::float8   as expires_ts
from mesh_approvals a
left join agent_registry r on r.name = a.approver;

-- La vue d'observabilité est recréée : `r.*` figeait les colonnes au moment de
-- sa création (0002), avant les colonnes de clé. On y ajoute `key_ready` pour
-- que `mesh list` distingue un agent dont la clé publique est enregistrée.
drop view if exists agent_mesh_overview;
create view agent_mesh_overview as
select
    r.*,
    extract(epoch from r.lease_expires_at)::float8 as lease_expires_ts,
    extract(epoch from r.last_seen)::float8        as last_seen_ts,
    extract(epoch from r.last_turn_at)::float8     as last_turn_ts,
    (r.public_key_fingerprint is not null and r.key_revoked_at is null) as key_ready,
    (select count(*) from agent_mailbox m
      where m.recipient = r.name and m.delivered_at is null) as unread
from agent_registry r;
