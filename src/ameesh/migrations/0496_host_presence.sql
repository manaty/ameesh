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
-- `host_resources` ; une tranche vidée reste à `readings = 0` (premier et
-- dernier instant nuls), la lecture l'ignore et la rétention de
-- `Hosts.record` l'efface avec les tranches de plus de 7 jours.
--
-- Concurrence (relecture #3921) : un recalcul lit un absolu ; il ne doit ni
-- croiser un autre recalcul ni une insertion de la même tranche. Chaque
-- tranche touchée est verrouillée (verrou consultatif de transaction, dans
-- un ordre stable) avant d'être écrite : partagé pour une insertion (les
-- cumuls se composent sous le verrou de ligne de ON CONFLICT), exclusif pour
-- un recalcul, qui relit ensuite `host_resources` avec un instantané pris
-- APRÈS le verrou (READ COMMITTED, celui des écrivains d'ameesh) : il voit
-- donc toute insertion ou suppression concurrente déjà validée.
--
-- Additif seulement : la table est remplie depuis l'historique existant,
-- lu par l'index (host, sampled_at) sans aller au tas ; aucun relevé n'est
-- modifié.

create table if not exists host_presence (
    host         text not null,
    -- début de tranche, aligné sur l'époque : floor(epoch(sampled_at)/120)*120
    bucket_start timestamptz not null,
    -- 0 : tranche vidée par une suppression, en attente de la rétention
    readings     bigint not null,
    first_at     timestamptz,
    last_at      timestamptz,
    primary key (host, bucket_start)
);

-- la rétention de `Hosts.record` efface par instant, tous hôtes confondus
create index if not exists host_presence_bucket_idx on host_presence (bucket_start);

create or replace function ameesh_presence_bucket(at timestamptz)
returns timestamptz language sql immutable parallel safe as $$
    select to_timestamp(floor(extract(epoch from at) / 120) * 120)
$$;

-- clé du verrou consultatif d'une tranche
create or replace function ameesh_presence_lock_key(host text, bucket timestamptz)
returns bigint language sql immutable parallel safe as $$
    select hashtextextended('host-presence:' || host || ':'
                            || extract(epoch from bucket)::bigint::text, 0)
$$;

create or replace function ameesh_host_presence_sync()
returns trigger language plpgsql as $$
declare
    hosts   text[];
    buckets timestamptz[];
    lock_key bigint;
begin
    if tg_op = 'INSERT' then
        for lock_key in
            select distinct ameesh_presence_lock_key(host, ameesh_presence_bucket(sampled_at))
              from presence_new order by 1
        loop
            perform pg_advisory_xact_lock_shared(lock_key);
        end loop;
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
    for lock_key in
        select distinct ameesh_presence_lock_key(t.host, t.bucket)
          from unnest(hosts, buckets) as t(host, bucket) order by 1
    loop
        perform pg_advisory_xact_lock(lock_key);
    end loop;
    -- instantané pris après les verrous : les écritures concurrentes validées
    -- de ces tranches sont vues ; une tranche vidée passe à 0
    insert into host_presence (host, bucket_start, readings, first_at, last_at)
    select t.host, t.bucket, count(r.sampled_at), min(r.sampled_at), max(r.sampled_at)
      from unnest(hosts, buckets) as t(host, bucket)
      left join host_resources r
        on r.host = t.host
       and r.sampled_at >= t.bucket
       and r.sampled_at < t.bucket + interval '120 seconds'
     group by t.host, t.bucket
    on conflict (host, bucket_start) do update
       set readings = excluded.readings,
           first_at = excluded.first_at,
           last_at  = excluded.last_at;
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
