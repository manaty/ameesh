-- SPDX-License-Identifier: AGPL-3.0-only
-- 0496_host_presence — #3921 : la disponibilité d'élection ne relit plus sept
-- jours de relevés.
--
-- Constat (pg_stat_statements de la base commune, 27/10 06:16-06:40Z) : la
-- série `SELECT extract(epoch FROM sampled_at) … ORDER BY sampled_at, id` de
-- `role_standings_pg.samples` a rendu 10 M lignes en 1 195 appels (8 400 par
-- appel, ~140 000 pour la fenêtre de 7 jours d'un hôte), à 312 ms par appel :
-- `election.availability` recalculait la couverture en Python à chaque
-- évaluation de chaque candidat.
--
-- `host_presence` résume `host_resources` par tranche de 120 s : nombre de
-- relevés, premier et dernier instant. La couverture de L165 (chaque relevé
-- couvre `cover_s`, borné par le suivant et par l'instant d'évaluation) se
-- calcule EXACTEMENT sur ces tranches tant que `cover_s` >= 120 s : deux
-- relevés d'une même tranche sont à moins de 120 s, l'intérieur d'une tranche
-- est donc couvert de bout en bout, et seuls les écarts ENTRE tranches sont
-- bornés par `cover_s`. Le seuil de fraîcheur ne descend jamais sous 180 s
-- (`host_lifecycle.MIN_STALENESS_S`). ~5 000 lignes étroites par hôte sur
-- 7 jours au lieu de ~140 000.
--
-- La table est tenue par des déclencheurs d'instruction (tables de
-- transition), pas par l'écrivain : des relevés sont aussi insérés, déplacés
-- ou effacés hors de `Hosts.record` (rétention, outils, bancs de test), et
-- une tranche ne doit jamais diverger de l'historique qu'elle résume.
-- Insertion : cumul incrémental (un upsert par tranche touchée). Mise à jour
-- et suppression : les tranches touchées sont recalculées depuis
-- `host_resources` (une tranche vidée est effacée) — la rétention de 7 jours
-- vide ainsi ses propres tranches.
--
-- Additif seulement : la table est remplie depuis l'historique existant,
-- lu par l'index (host, sampled_at) sans aller au tas ; aucun relevé n'est
-- modifié.

create table if not exists host_presence (
    host         text not null,
    -- début de tranche, aligné sur l'époque : floor(epoch(sampled_at)/120)*120
    bucket_start timestamptz not null,
    readings     bigint not null,
    first_at     timestamptz not null,
    last_at      timestamptz not null,
    primary key (host, bucket_start)
);

create or replace function ameesh_presence_bucket(at timestamptz)
returns timestamptz language sql immutable parallel safe as $$
    select to_timestamp(floor(extract(epoch from at) / 120) * 120)
$$;

create or replace function ameesh_host_presence_sync()
returns trigger language plpgsql as $$
declare
    hosts   text[];
    buckets timestamptz[];
begin
    if tg_op = 'INSERT' then
        insert into host_presence (host, bucket_start, readings, first_at, last_at)
        select host, ameesh_presence_bucket(sampled_at), count(*),
               min(sampled_at), max(sampled_at)
          from presence_new
         group by 1, 2
        on conflict (host, bucket_start) do update
           set readings = host_presence.readings + excluded.readings,
               first_at = least(host_presence.first_at, excluded.first_at),
               last_at  = greatest(host_presence.last_at, excluded.last_at);
        return null;
    end if;
    -- UPDATE, DELETE : les tranches touchées (avant et après) sont recalculées
    if tg_op = 'UPDATE' then
        select array_agg(k.host), array_agg(k.bucket) into hosts, buckets
          from (select host, ameesh_presence_bucket(sampled_at) as bucket from presence_old
                union
                select host, ameesh_presence_bucket(sampled_at) from presence_new) k;
    else
        select array_agg(k.host), array_agg(k.bucket) into hosts, buckets
          from (select distinct host, ameesh_presence_bucket(sampled_at) as bucket
                  from presence_old) k;
    end if;
    if hosts is null then
        return null;
    end if;
    insert into host_presence (host, bucket_start, readings, first_at, last_at)
    select t.host, t.bucket, count(*), min(r.sampled_at), max(r.sampled_at)
      from unnest(hosts, buckets) as t(host, bucket)
      join host_resources r
        on r.host = t.host
       and r.sampled_at >= t.bucket
       and r.sampled_at < t.bucket + interval '120 seconds'
     group by t.host, t.bucket
    on conflict (host, bucket_start) do update
       set readings = excluded.readings,
           first_at = excluded.first_at,
           last_at  = excluded.last_at;
    delete from host_presence p
     using unnest(hosts, buckets) as t(host, bucket)
     where p.host = t.host and p.bucket_start = t.bucket
       and not exists (select 1 from host_resources r
                        where r.host = t.host
                          and r.sampled_at >= t.bucket
                          and r.sampled_at < t.bucket + interval '120 seconds');
    return null;
end
$$;

create or replace trigger host_presence_insert
    after insert on host_resources
    referencing new table as presence_new
    for each statement execute function ameesh_host_presence_sync();

create or replace trigger host_presence_update
    after update on host_resources
    referencing old table as presence_old new table as presence_new
    for each statement execute function ameesh_host_presence_sync();

create or replace trigger host_presence_delete
    after delete on host_resources
    referencing old table as presence_old
    for each statement execute function ameesh_host_presence_sync();

-- remplissage : dans la même transaction que les déclencheurs (aucun relevé
-- ne peut s'insérer entre les deux), depuis l'index (host, sampled_at)
insert into host_presence (host, bucket_start, readings, first_at, last_at)
select host, ameesh_presence_bucket(sampled_at), count(*),
       min(sampled_at), max(sampled_at)
  from host_resources
 group by 1, 2
on conflict (host, bucket_start) do nothing;

comment on table host_presence is
    '#3921: host_resources summarised per 120-second bucket (readings, first and last instant) — the 7-day election availability is computed on ~5 000 rows per host instead of the whole history. Maintained by statement triggers on host_resources.';
