-- SPDX-License-Identifier: AGPL-3.0-only
-- 0495_listing_index — #3923 : la lecture du registre de `operations.listing`
-- ne relit plus ni le tas du courrier, ni celui des lots pour chaque agent.
--
-- Constat (27/10, base commune) : `operations.listing` (SELECT r.name,
-- r.chantier, r.team …), appelé par la prise automatique de chaque exécuteur,
-- coûtait 1,2 à 1,4 s et 630 appels en 4 minutes. Deux postes, sur 51 agents :
--   * la latérale de courrier relisait dans le tas chaque non-lu pour évaluer
--     le prédicat passif, soit 443 ms pour les seuls 5 074 non-lus de `bureau`;
--     l'index 0489 ne couvre que les non-lus PASSIFS de la vue
--     `agent_mesh_overview`, les ACTIFS du listing n'avaient aucun index ;
--   * les trois latérales de lots (dernier lot assigné, lots actifs, lots
--     actionnables) parcouraient le tas pour chaque agent (≈ 480 ms) et la
--     relecture d'un lot en qa sans verdict refaisait un parcours par état.
--
-- Trois index partiels, tous additifs, sans changement de vue, de table ni de
-- ligne :
--   * `agent_mailbox_unread_active_idx` : prédicat identique, au mot près, à la
--     négation de `storage.postgres.mailbox.PASSIVE_SQL` ; le planificateur
--     prouve l'inclusion et compte par un parcours d'index seul (443 ms -> ~8 ms
--     pour les 5 074 lignes de `bureau`, sur la base commune). `include
--     (created_at)` sert le `min(created_at)` de la même latérale sans revenir
--     au tas. Un changement du prédicat passif change aussi cet index (une
--     migration de plus), sinon le compte retombe sur le tas sans rien casser.
--   * `work_items_assignee_recent_idx` : le dernier lot assigné se lit dans
--     l'index (state, planned_start et title inclus), et les comptes de lots
--     actifs/actionnables s'y lisent aussi sans le tas.
--   * `work_items_reviewer_state_idx` : la relecture désignée due (#1726) part
--     du relecteur au lieu de parcourir tous les lots en qa.
--   * `turn_costs_agent_recent_idx` : le dernier tour de chaque agent se lit
--     dans l'index (contexte relu et session inclus), sans retour au tas.
--
-- Construction simple, pas `CONCURRENTLY` (`migrate()` tourne dans une
-- transaction, comme 0489) ; la messagerie et les lots sont très écrits, d'où
-- une attente de verrou bornée à 5 s : la migration échoue et reste en attente
-- plutôt que de faire patienter chaque écrivain derrière la construction.
set local lock_timeout = '5s';

create index if not exists agent_mailbox_unread_active_idx
  on agent_mailbox (recipient, id) include (created_at)
  where delivered_at is null
    and (NOT (NOT agent_mailbox.payload @> '{"urgent": true}'::jsonb
              AND (agent_mailbox.payload @> '{"ack": true}'::jsonb
               OR agent_mailbox.payload @> '{"broadcast": true}'::jsonb
               OR agent_mailbox.payload @> '{"ci_passif": true}'::jsonb
               OR agent_mailbox.payload @> '{"info": true}'::jsonb
               OR coalesce(agent_mailbox.payload->>'cc', '') <> ''
               OR coalesce(jsonb_typeof(agent_mailbox.payload->'radar'), '') = 'object')));

create index if not exists work_items_assignee_recent_idx
  on work_items (assignee, updated_at desc, id desc)
  include (state, planned_start, title)
  where assignee is not null;

create index if not exists work_items_reviewer_state_idx
  on work_items (reviewer, state)
  where reviewer is not null;

-- Le dernier tour de chaque agent (contexte relu, session) se lit dans
-- l'index : la latérale `t` du listing est le dernier gros poste (48 766
-- tours, 31 Mo) ; `agent_recorded` (0481) porte les mêmes clés, celui-ci
-- ajoute les colonnes lues pour éviter le retour au tas (109 ms -> 7 ms
-- sur les 51 agents, plan du 27/10).
create index if not exists turn_costs_agent_recent_idx
  on turn_costs (agent, recorded_at desc, id desc)
  include (harness, input_tokens, cached_input_tokens, session);
