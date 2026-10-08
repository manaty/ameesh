-- SPDX-License-Identifier: AGPL-3.0-only
-- 0032_plusieurs_canons — plusieurs canons sur un même hôte (lot L42,
-- décision 0031).
--
-- Un hôte lit une LISTE de canons ; le premier est le canon PAR DÉFAUT. Un
-- canon est identifié par l'`id` de sa fédération (sinon le nom de son
-- dossier). Chaque synchronisation ne touche que les lignes de SON canon, et la
-- réclamation se ferme par canon, plus par hôte.
--
-- 1. `agent_registry.canon`, `work_packages.canon` : le canon qui a déclaré la
--    ligne. NULL = le canon par défaut : toutes les lignes existantes, et
--    celles que le canon par défaut écrit (aucun changement de données pour
--    un hôte à un seul canon). Un autre canon y écrit son identifiant. Un
--    éphémère reçoit le canon de son créateur (`agent spawn`).
--
-- 2. `canon_state` : la clé devient (host, canon). `canon` = '' pour le canon
--    par défaut (la ligne existante de chaque hôte, inchangée), l'identifiant
--    du canon sinon. `canon_id` garde l'identifiant RÉEL du canon qui a écrit
--    la ligne (pour la ligne '' : celui du canon par défaut, NULL tant qu'une
--    synchronisation ne l'a pas lu dans federation.yaml) : il permet de
--    reconnaître un changement de canon par défaut.
--
-- 3. Condition de réclamation (`registry.canon_claim_predicate_sql`) : l'état
--    `ok` de l'hôte de l'agent POUR SON CANON (`coalesce(canon, '')`). Un
--    canon invalide ne ferme plus que ses propres agents (et leurs
--    éphémères).
--
-- 4. La vue d'observabilité fige ses colonnes (`r.*` développé à la
--    création) : recréée à l'identique de 0029 pour exposer `canon`.

-- 1. ---------------------------------------------------------- lignes
alter table agent_registry add column if not exists canon text;
alter table work_packages  add column if not exists canon text;

comment on column agent_registry.canon is
  'L42 (0031) : canon déclarant (id de sa fédération) ; NULL = canon par défaut de l''hôte.';
comment on column work_packages.canon is
  'L42 (0031) : canon déclarant (id de sa fédération) ; NULL = canon par défaut.';

-- 2. ---------------------------------------------------------- état
alter table canon_state
    add column if not exists canon    text not null default '',
    add column if not exists canon_id text;
alter table canon_state drop constraint if exists canon_state_pkey;
alter table canon_state add constraint canon_state_pkey primary key (host, canon);

comment on column canon_state.canon is
  'L42 (0031) : '''' = canon par défaut de l''hôte ; sinon l''id du canon.';
comment on column canon_state.canon_id is
  'L42 (0031) : id réel du canon qui a écrit la ligne (NULL : inconnu, ligne antérieure).';

-- 4. ---------------------------------------------------------- vue
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
