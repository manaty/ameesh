-- SPDX-License-Identifier: AGPL-3.0-only
-- 0488_base_commune_cout_requetes — #3631 : les lectures périodiques de la
-- base commune ne relisent plus tout l'historique.
--
-- Constat (analyse profonde du 26/10, angle 4) : ~1 080 « statement timeout »
-- en 24 h sur la base commune, 639 passes de redésignation des relecteurs sans
-- effet, ~390 échecs de synchro GitHub repris, 84 coupures LISTEN. Les relevés
-- d'hôtes et le journal du ménage sont relus par CHAQUE exécuteur à chaque
-- passe (`lend_basis` sur 7 jours, `detect_losses`, `last_measure`,
-- `latest_by_path`) : plusieurs centaines de Mo par appel, contre 128 Mo de
-- `shared_buffers`, d'où la pression qui fait expirer des requêtes sans
-- rapport.
--
-- 1. `host_resource_slots` : les relevés de `host_resources` agrégés par
--    tranche de 10 min (nombre de relevés, somme/effectif de `app_cpu`,
--    minimum de mémoire libre, premier/dernier instant mesuré). `lend_basis`
--    lit ~1 000 lignes par hôte au lieu de ~135 000 lignes larges ; la
--    sémantique de L159d est conservée (nombre de relevés, étendue des
--    relevés mesurés, minimum, moyenne par tranche), au bord de fenêtre près
--    (une tranche).
-- 2. Deux index de `housekeeping_log` : `last_measure` et `latest_by_path`
--    lisent les identifiants par l'index (kind, hôte, chemin, instant) et ne
--    vont au tas que pour la ligne retenue de chaque chemin.
--
-- Additif seulement : la table est remplie depuis l'historique existant ;
-- aucun relevé ni journal n'est modifié. L'écrivain (`Hosts.record`) la tient
-- à jour dans la même transaction que le relevé.

-- 1. ---------------------------------------------------- tranches de relevés
create table if not exists host_resource_slots (
    host        text not null,
    -- début de tranche, aligné sur l'époque : floor(epoch(sampled_at)/600)*600
    slot_start  timestamptz not null,
    -- nombre de relevés de la tranche, mesurés ou non : `samples` de L159d.
    readings    bigint not null default 0,
    -- somme et effectif de `app_cpu` : la moyenne de la tranche est
    -- cpu_sum / cpu_n, exactement comme `avg(app_cpu)` groupé par tranche.
    cpu_sum     double precision not null default 0,
    cpu_n       bigint not null default 0,
    -- minimum de `mem_available_bytes + guest_mem_bytes` de la tranche.
    free_mem_min bigint,
    -- premier/dernier relevé MESURÉ (app_cpu non nul) : l'étendue de L159d.
    first_cpu_at timestamptz,
    last_cpu_at  timestamptz,
    primary key (host, slot_start)
);

insert into host_resource_slots
    (host, slot_start, readings, cpu_sum, cpu_n, free_mem_min,
     first_cpu_at, last_cpu_at)
select host,
       to_timestamp(floor(extract(epoch from sampled_at) / 600.0) * 600.0),
       count(*),
       coalesce(sum(app_cpu), 0),
       count(app_cpu),
       min(mem_available_bytes + guest_mem_bytes),
       min(sampled_at) filter (where app_cpu is not null),
       max(sampled_at) filter (where app_cpu is not null)
  from host_resources
 group by host, 2
on conflict (host, slot_start) do nothing;

-- 2. -------------------------------------------------- journal du ménage
-- `last_measure` : DISTINCT ON (host) ... WHERE kind = 'mesure'
-- ORDER BY host, at DESC, id DESC — l'index porte cet ordre, la lecture des
-- identifiants se fait donc sans tri ni accès au tas (voir `last_measure`).
create index if not exists housekeeping_log_kind_host_time_idx
    on housekeeping_log (kind, host, at desc, id desc);

-- `latest_by_path` : DISTINCT ON (host, path) ... WHERE kind = %s
-- ORDER BY host, path, at DESC, id DESC — même principe, un accès au tas par
-- chemin retenu au lieu d'un par ligne.
create index if not exists housekeeping_log_kind_host_path_time_idx
    on housekeeping_log (kind, host, path, at desc, id desc);

comment on table host_resource_slots is
    '#3631: host_resources aggregated per 10-minute slot — the historical basis of lend_basis, read in ~1 000 rows per host instead of the whole 7-day, wide history. Maintained by Hosts.record in the reading transaction.';
