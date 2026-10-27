-- SPDX-License-Identifier: AGPL-3.0-only
-- 0494_work_events_created_at_idx — #3920 : le compte des prises de l'heure
-- ne relit plus tout le journal des lots.
--
-- Constat (27/10, base commune, pg_stat_statements) : `SELECT count(*) FROM
-- work_item_events WHERE created_at > now() - … AND starts_with(note, …)`,
-- 3e requête au temps total, 2 720 appels à 866 ms en moyenne. Aucun index ne
-- couvre `created_at` : chaque appel parcourt les ~77 000 lignes du journal
-- pour en garder les ~340 de la dernière heure (1,1 s mesurée, tout en cache).
-- Appelants : `WorkStore.auto_takes_since` (places restantes de backlog.py,
-- relues avant chaque prise, #804, et de handoff.py) et `WorkStore.handoff`,
-- sous le verrou de remise (`_HANDOFF_LOCK`), qui faisait donc attendre les
-- autres remises une seconde de plus. La relecture par prise est voulue
-- (compteur partagé entre exécuteurs) : on rend la lecture bon marché plutôt
-- que de la mettre en cache.
--
-- Index simple sur `created_at` : la fenêtre (une heure) se lit par parcours
-- d'intervalle, le préfixe de note se filtre sur ces seules lignes.
--
-- Additif : ni vue, ni table, ni ligne modifiée. Construction simple, pas
-- `CONCURRENTLY` (`migrate()` tourne dans une transaction, comme 0489) ; le
-- journal est écrit à chaque transition de lot, d'où une attente de verrou
-- bornée à 5 s : la migration échoue et reste en attente plutôt que de faire
-- patienter chaque écrivain derrière la construction.
set local lock_timeout = '5s';

create index if not exists work_item_events_created_at_idx
  on work_item_events (created_at);
