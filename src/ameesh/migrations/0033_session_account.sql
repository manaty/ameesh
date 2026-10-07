-- SPDX-License-Identifier: AGPL-3.0-only
-- 0033_session_account — compte d'origine de la session courante
-- (lot L39, décision 0030 point 3 « adoption et reprise sont des opérations
-- d'ameesh »).
--
-- `agent_registry.session_account` : le compte (nom déclaré dans `accounts`
-- de l'hôte, décision 0027) sous lequel la session courante (`session_id`) a
-- été ouverte, ou reprise en dernier. L'exécuteur l'écrit avec l'id de
-- session (`registry.set_session`) ; `ameesh adopt` l'écrit pour une session
-- interactive adoptée (compte où son fichier a été trouvé). Avant L39, ce
-- compte ne se lisait que dans le marqueur du flux `events.jsonl` de
-- l'exécuteur, qu'une session adoptée n'a pas : `account_turn` lit d'abord
-- cette colonne, puis le marqueur (compatibilité).
--
-- NULL : compte inconnu (hôte sans comptes déclarés, ou session antérieure).
-- Le déclencheur ci-dessous l'efface dès que la session est oubliée
-- (`session_id` NULL : rotation, `ameesh restart`, `ameesh resume --fresh`,
-- déplacement non portable…), quel que soit le chemin qui l'oublie.

alter table agent_registry add column if not exists session_account text;

create or replace function ameesh_session_account() returns trigger
language plpgsql as $$
begin
    if new.session_id is null then
        new.session_account := null;
    end if;
    return new;
end $$;

drop trigger if exists agent_registry_session_account on agent_registry;
create trigger agent_registry_session_account
    before insert or update of session_id, session_account on agent_registry
    for each row execute function ameesh_session_account();

-- La vue d'observabilité fige ses colonnes (`r.*` développé à la création) :
-- recréée à l'identique de 0030, augmentée de `session_account`.
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
