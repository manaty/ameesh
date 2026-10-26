-- SPDX-License-Identifier: AGPL-3.0-only
-- 0489_courrier_passif_index — #3741 : le compte des non-lus passifs de la
-- vue `agent_mesh_overview` ne relit plus le tas du courrier.
--
-- Constat (26/10, base commune, ~90 relevés de pg_stat_activity) : la lecture
-- `registry.overview` occupait 40 % des requêtes actives, ~16 exemplaires en
-- même temps, 1,7 s chacun sous charge. Sa colonne `passive_unread` relit,
-- pour chaque agent, toutes ses lignes non lues dans le tas afin d'évaluer le
-- prédicat passif sur `payload` : 333 ms des 341 ms de la vue, dont l'essentiel
-- pour les 4 836 non-lus de `bureau`. La pression qui en résulte fait expirer
-- des requêtes sans rapport (canon sync, réveils programmés, sync-merges).
--
-- Index partiel sur les non-lus PASSIFS, prédicat identique, au mot près, à
-- celui de la vue (0483) et de `storage.postgres.mailbox.PASSIVE_SQL` : le
-- planificateur prouve l'inclusion et compte par un parcours d'index seul
-- (2,4 ms pour la vue entière au lieu de 240 ms, sur une copie de la
-- messagerie). Un changement du prédicat passif change aussi cet index (une
-- migration de plus), sinon le compte retombe sur le tas sans rien casser.
--
-- Additif : ni vue, ni table, ni ligne modifiée. Construction simple, pas
-- `CONCURRENTLY` (`migrate()` tourne dans une transaction, comme 0413) ; la
-- messagerie est très écrite, d'où une attente de verrou bornée à 5 s : la
-- migration échoue et reste en attente plutôt que de faire patienter chaque
-- écrivain derrière la construction.
set local lock_timeout = '5s';

create index if not exists agent_mailbox_passive_unread_idx
  on agent_mailbox (recipient, id)
  where delivered_at is null
    and (NOT payload @> '{"urgent": true}'::jsonb
         AND (payload @> '{"ack": true}'::jsonb
          OR payload @> '{"broadcast": true}'::jsonb
          OR payload @> '{"ci_passif": true}'::jsonb
          OR payload @> '{"info": true}'::jsonb
          OR coalesce(payload->>'cc', '') <> ''
          OR coalesce(jsonb_typeof(payload->'radar'), '') = 'object'));
