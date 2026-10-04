-- SPDX-License-Identifier: AGPL-3.0-only
-- 0022_placement — placement gouverné (C4, R18 ; spec §5 « Réclamation », lot L3).
--
-- Colonnes DÉCLARATIVES, écrites par `ameesh canon sync` pour chaque agent du
-- canon de l'hôte synchronisé (et recopiées par `ameesh agent spawn` du
-- créateur vers l'éphémère) ; jamais par l'exécuteur. Elles ne remplacent
-- aucune colonne d'état (bail, session, statut, dépense, consigne).
--
--   placement_ok          le placement de l'agent sur SON hôte est admis par
--                         la politique de la fiche Host (`canon.placement_violations`) ;
--                         faux si la politique le refuse, si l'agent n'a pas
--                         de placement sur cet hôte, si le placement est
--                         ambigu ou la politique introuvable ; NULL tant que
--                         rien ne l'a évalué (agent inscrit à la main, agent
--                         du canon pas encore synchronisé depuis cette
--                         migration, agent déplacé vers un hôte pas encore
--                         synchronisé)
--   placement_diagnostic  la raison du refus, lisible ('' si admis)
--   placement_ref         `<membre>:<chemin>@<commit>` de la fiche Placement lue
--                         (celle du créateur racine pour un éphémère)
--
-- La condition de réclamation (`registry.canon_claim_predicate_sql`) exige
-- `placement_ok` pour un agent gouverné par le canon : NULL ou faux, pas de
-- NOUVELLE réclamation (fail closed). Baux, sessions et tours en cours ne sont
-- pas touchés. Les agents inscrits à la main ne sont pas concernés. Après
-- cette migration, un `ameesh canon sync` par hôte rouvre la réclamation des
-- agents du canon dont le placement est admis.

alter table agent_registry
    add column if not exists placement_ok         boolean,
    add column if not exists placement_diagnostic text,
    add column if not exists placement_ref        text;

-- Le verdict vaut pour un hôte. Un changement d'hôte qui ne vient pas d'une
-- réévaluation (`ameesh run register` ou `import-v0` sur une autre machine,
-- SQL à la main) n'emporte pas le verdict : il est remis à NULL, donc plus de
-- réclamation avant le `canon sync` de ce nouvel hôte. `canon sync` change
-- l'hôte en écrivant la fiche Placement de l'hôte cible (`placement_ref`
-- différent) : il n'est pas concerné. Un agent jamais évalué (inscrit à la
-- main) non plus.
create or replace function agent_registry_placement_host() returns trigger
language plpgsql as $$
begin
    if new.host is distinct from old.host
       and new.placement_ref is not distinct from old.placement_ref
       and old.placement_ok is not null then
        new.placement_ok := null;
        new.placement_diagnostic := 'hôte changé hors de « ameesh canon sync » ('
            || coalesce(old.host, '') || ' → ' || coalesce(new.host, '')
            || ') : placement à réévaluer par « ameesh canon sync » sur '
            || coalesce(new.host, '');
    end if;
    return new;
end
$$;

drop trigger if exists agent_registry_placement_host on agent_registry;
create trigger agent_registry_placement_host
    before update of host on agent_registry
    for each row execute function agent_registry_placement_host();

-- La vue d'observabilité fige ses colonnes (`r.*` est développé à la
-- création) : on la recrée, à l'identique de 0008, pour qu'elle expose les
-- colonnes du placement (la condition de réclamation est évaluée sur elle par
-- `registry.overview`).
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
