-- SPDX-License-Identifier: AGPL-3.0-only
-- 0523_turn_costs_turn_id_idx — #4850 : la lecture d'un tour dans le grand livre
-- ne parcourt plus toute la table.
--
-- Constat (30/10, base commune, 60 149 lignes dont 44 670 avec `turn_id`) :
-- `LEDGER_SQL` de l'observation d'un tour (#4831, `storage.postgres.
-- turn_observation`) filtre sur `turn_id` ou `spend_key`, trié par `id` avec
-- `LIMIT 1`. `turn_id` n'ayant aucun index, le planificateur remonte la clé
-- primaire et filtre : immédiat pour un tour récent, mais un tour absent ou
-- ancien lit toute la table (830 ms, 49 596 tampons, 60 149 lignes écartées).
--
-- Mesure sur une copie temporaire de la table, avec cet index : BitmapOr de cet
-- index et de `turn_costs_spend_key`, 0,13 ms et 6 tampons pour un tour absent,
-- le même plan en plan générique (`turn_id = $1`) ; index de 2,6 Mo.
--
-- Partiel : les lignes sans `turn_id` (d'avant 0423, relais, appareils) ne
-- gonflent pas l'index. Additif, sans changement de vue, de table ni de ligne.
-- Construction simple, pas `CONCURRENTLY` (`migrate()` tourne dans une
-- transaction, comme 0495) ; le grand livre est écrit à chaque tour, d'où la
-- même attente de verrou bornée à 5 s.
set local lock_timeout = '5s';

create index if not exists turn_costs_turn_id_idx
  on turn_costs (turn_id)
  where turn_id is not null;
