-- SPDX-License-Identifier: AGPL-3.0-only
-- 0533_requetes_lourdes_alertes — #5411 : les trois lectures lourdes du
-- passage d'alertes lisent l'index au lieu de parcourir le journal.
--
-- Constat (#5404, 23:42Z) : une fois `alerts` allégé côté code, le reste de la
-- latence vient de la base commune — 3,2 Go de données pour 128 Mo de
-- `shared_buffers`, 12 connexions actives en moyenne, pointes à 42, cache à
-- 90 %. `pg_stat_statements` depuis le 27/10 désigne trois familles de
-- requêtes :
--
-- 1. `measure_candidates` (#5411, 197 000 s cumulées, 3,8 s d'appel) : pour
--    chaque lot ouvert démarré, le LATERAL cherchait le jalon `merged` dans
--    `work_item_milestones_item_idx (work_item_id, at)` puis filtrait `kind` ;
--    il lisait ~26 tampons par lot (111 931 tampons mesurés), dont 4 289
--    lots pour 5 candidats retenus. L'index (work_item_id, kind, at, id)
--    donne le jalon en un accès.
-- 2. `housekeeping_log.latest_by_path` avec fenêtre (50 000 s cumulées,
--    3,6 s mesurés) : `kind = %s AND at >= now() - 24h` ne pouvait lire aucun
--    index (`at` n'est pas la première colonne de
--    `housekeeping_log_kind_host_path_time_idx`) et la base scannait 762 848
--    lignes puis triait 56 712 lignes.
-- 3. `provider_balances` : voir `Operations.balances` (#5411), la borne
--    `observed_at < cutoff` de la sonde évite de relire tout l'historique
--    pour les séries déjà couvertes par la fenêtre.
--
-- Additif seulement : deux index, aucune donnée ni colonne modifiée.

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
    '#5411: latest_by_path with a time window (e.g. tmp/24 h) reads the window in index order instead of scanning the whole journal.';
