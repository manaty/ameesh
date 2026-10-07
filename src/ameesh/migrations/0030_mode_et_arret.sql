-- SPDX-License-Identifier: AGPL-3.0-only
-- 0030_mode_et_arret — mode d'agent et raison d'arrêt structurée
-- (lot L37, décision 0030 « pas de travail sans réveil possible »).
--
-- 1. `agent_registry.mode` : `execute` (mené par un exécuteur, sous bail) ou
--    `externe` (session humaine, boîte seulement, jamais réveillée par
--    ameesh). Colonne, pas texte libre : l'attribution gardée (un lot ne va
--    qu'à un agent réveillable) et les alertes de vivacité la lisent. Un
--    agent créé par un hook SANS bail naît `externe` ; un agent existant
--    n'est jamais basculé par un hook (`ameesh set <agent> mode=…`).
-- 2. `agent_registry.stop_reason` : pourquoi l'agent est `stopped` / `dead` —
--    `manuel` (`agent-runner stop`), `bail_expire` (tour mort, bail échu),
--    `retire_du_canon` (`canon sync`), `externe` (session humaine close),
--    `erreur`. NULL tant que l'agent tourne : le déclencheur ci-dessous
--    l'efface dès que le statut quitte `stopped` / `dead` (claim, consigne,
--    réintégration…), quel que soit le chemin qui relance l'agent.
--    `stopped_with_mail` ne se tait que pour un arrêt `manuel`.

alter table agent_registry add column if not exists mode text not null default 'execute';
alter table agent_registry drop constraint if exists agent_registry_mode_check;
alter table agent_registry add constraint agent_registry_mode_check
    check (mode in ('execute', 'externe'));

alter table agent_registry add column if not exists stop_reason text;
alter table agent_registry drop constraint if exists agent_registry_stop_reason_check;
alter table agent_registry add constraint agent_registry_stop_reason_check
    check (stop_reason is null
           or stop_reason in ('manuel', 'bail_expire', 'retire_du_canon', 'externe', 'erreur'));

-- Les arrêts déjà en base reçoivent la raison que disait leur texte libre.
update agent_registry set stop_reason = case
        when status_text like 'arrêté à la main%' then 'manuel'
        when status_text like 'bail expiré%'      then 'bail_expire'
        when status_text like 'retiré du canon%'  then 'retire_du_canon'
    end
 where status in ('stopped', 'dead') and stop_reason is null;

create or replace function ameesh_stop_reason() returns trigger
language plpgsql as $$
begin
    if new.status not in ('stopped', 'dead') then
        new.stop_reason := null;
    end if;
    return new;
end $$;

drop trigger if exists agent_registry_stop_reason on agent_registry;
create trigger agent_registry_stop_reason
    before insert or update of status, stop_reason on agent_registry
    for each row execute function ameesh_stop_reason();

-- La vue d'observabilité fige ses colonnes (`r.*` développé à la création) :
-- recréée à l'identique de 0029, augmentée de `mode` et `stop_reason`.
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
