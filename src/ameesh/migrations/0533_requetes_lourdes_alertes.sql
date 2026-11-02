-- SPDX-License-Identifier: AGPL-3.0-only
-- 0533_requetes_lourdes_alertes — #5411 : les trois lectures lourdes du
-- passage d'alertes lisent l'index au lieu de parcourir le journal.
--
-- Constat (#5404, 23:42Z) : une fois `alerts` allégé côté code, le reste de la
-- latence vient de la base commune — 3,4 Go de données pour 128 Mo de
-- `shared_buffers`, 12 connexions actives en moyenne, pointes à 42, cache à
-- 90 %. `pg_stat_statements` depuis le 27/10 désigne trois familles de
-- requêtes :
--
-- 1. `measure_candidates` (197 000 s cumulées, 3,8 s d'appel) : pour chaque
--    lot ouvert démarré, le LATERAL cherchait le jalon `merged` dans
--    `work_item_milestones_item_idx (work_item_id, at)` puis filtrait `kind` ;
--    il lisait ~26 tampons par lot (111 931 tampons mesurés), dont 4 289 lots
--    pour 5 candidats retenus. L'index (work_item_id, kind, at, id) donne le
--    jalon en un accès.
-- 2. `housekeeping_log.latest_by_path` avec fenêtre (50 000 s cumulées,
--    3,6 s mesurés) : `kind = %s AND at >= now() - 24h` ne pouvait lire aucun
--    index (`at` n'est pas la première colonne de
--    `housekeeping_log_kind_host_path_time_idx`) et la base scannait 762 848
--    lignes puis triait 56 712 lignes. L'index (kind, at desc, host, path,
--    id desc) lit la fenêtre dans l'ordre du temps ; le plan ne le choisit
--    qu'avec un `random_page_cost` de SSD (mesure ci-dessous).
-- 3. `provider_balances` : voir `Operations.balances` et `balance_series`
--    (#5411) — la borne `observed_at < cutoff` et l'agrégat par devise
--    évitent de relire tout l'historique pour chaque appel.
--
-- Mesure de construction sur une copie temporaire de `housekeeping_log`
-- (764 282 lignes, 799 Mo, base commune du 02/11 01:0xZ) : index de 109 Mo
-- construit en 22,3 s ; l'index des jalons (30 195 lignes) en 0,5 s.
--
-- Mesure du plan sur la même copie (EXPLAIN ANALYZE, BUFFERS) :
--   * sans index : Parallel Seq Scan + tri externe, 4,3 à 10 s à froid,
--     402 008 tampons lus ;
--   * avec index et `random_page_cost = 4` (défaut, disque tournant) : le
--     planificateur préfère encore le Seq Scan ;
--   * avec index et `random_page_cost = 1.1` (SSD, recommandation #5411) :
--     Index Only Scan de la fenêtre, 1,55 s et 29 514 tampons lus.
-- Le bureau applique l'index et le `random_page_cost` à la même coupure
-- staging ; sans le second, cet index reste ignoré par le planificateur.
--
-- Additif seulement : deux index, aucune donnée ni colonne modifiée.
-- Construction simple, pas CONCURRENTLY (`migrate()` tourne dans une
-- transaction, comme 0495 et 0523) ; `housekeeping_log` est écrit à chaque
-- passe, d'où une attente de verrou bornée à 5 s : la migration échoue et
-- reste en attente plutôt que de faire patienter chaque écrivain derrière la
-- construction. Le temps de construction mesuré (22 s) demande de la placer à
-- une coupure staging, pas en pleine passe.
set local lock_timeout = '5s';

-- 1. ------------------------------------------- jalon de fusion par lot
-- Même nom et même définition que l'index posé en essai dans qa5404_probe :
-- une seule migration le crée, l'autre est un no-op (`if not exists`).
create index if not exists work_item_milestones_item_kind_at_idx
    on work_item_milestones (work_item_id, kind, at desc, id desc);

-- 2. --------------------------------------- journal du ménage, par fenêtre
-- `latest_by_path` avec `since_s` lit les seules lignes de la fenêtre dans
-- l'ordre du temps, puis garde la dernière par (hôte, chemin). `id` est la
-- dernière colonne pour départager deux relevés au même instant, comme le
-- `ORDER BY at DESC, id DESC` de la requête.
create index if not exists housekeeping_log_kind_at_host_path_idx
    on housekeeping_log (kind, at desc, host, path, id desc);

comment on index housekeeping_log_kind_at_host_path_idx is
    '#5411: latest_by_path with a time window (e.g. tmp/24 h) reads the window in index order instead of scanning the whole journal; picked once random_page_cost reflects the SSD.';
