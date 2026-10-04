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
--   placement_profile     le PROFIL ÉVALUÉ : `ameesh_placement_profile(hôte,
--                         harnais, fournisseur, modèle, mode d'identifiants)`
--                         des valeurs que le verdict a jugées (celles que sync
--                         écrit, ou celles du créateur pour un éphémère) ;
--                         écrit avec chaque verdict, jamais seul
--
-- Le verdict ne vaut que pour le profil évalué. La condition de réclamation
-- (`registry.canon_claim_predicate_sql`) exige, pour un agent gouverné par le
-- canon, `placement_ok` ET `placement_profile` égal au profil COURANT de la
-- ligne (`ameesh_placement_profile` de ses colonnes). Verdict NULL ou faux, ou
-- profil divergé depuis l'évaluation — `ameesh run register` sur une autre
-- machine ou avec un autre harnais, `import-v0`, SQL à la main —, pas de
-- NOUVELLE réclamation (fail closed) jusqu'au prochain `ameesh canon sync`,
-- sans déclencheur à deviner qui écrit. Baux, sessions et tours en cours ne
-- sont pas touchés. Les agents inscrits à la main ne sont pas concernés.
-- Après cette migration, un `ameesh canon sync` par hôte rouvre la
-- réclamation des agents du canon dont le placement est admis.

alter table agent_registry
    add column if not exists placement_ok         boolean,
    add column if not exists placement_diagnostic text,
    add column if not exists placement_ref        text,
    add column if not exists placement_profile    text;

-- Profil d'un placement, forme canonique : chaque valeur citée par
-- quote_nullable (apostrophes doublées), NULL écrit `NULL` — distinct de la
-- chaîne vide `''` —, dans un ordre fixe. Jamais NULL (pas STRICT, pas de
-- concaténation avec NULL) : deux profils inconnus ne sont jamais « égaux »
-- par accident. Immuable : même entrée, même texte, sur toutes les bases.
create or replace function ameesh_placement_profile(
    p_host text, p_harness text, p_provider text, p_model text,
    p_credential_mode text)
returns text
language sql immutable parallel safe
as $$
    select 'host=' || quote_nullable(p_host)
        || ' harness=' || quote_nullable(p_harness)
        || ' provider=' || quote_nullable(p_provider)
        || ' model=' || quote_nullable(p_model)
        || ' credential_mode=' || quote_nullable(p_credential_mode)
$$;

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
