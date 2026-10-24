-- SPDX-License-Identifier: AGPL-3.0-only
-- 0481_turn_costs_agent_recorded — #3397 : le dernier tour d'un agent, sans
-- balayer tout l'historique.
--
-- `operations.listing()` (registre, `/api/v1/agents`, `ameesh alerts`) joint
-- pour chaque agent son dernier `turn_costs` avec
-- `WHERE agent = r.name ORDER BY recorded_at DESC, id DESC LIMIT 1`.
-- L'index existant `turn_costs_agent_time_idx` porte `harness` entre `agent`
-- et `recorded_at` : il ne peut pas servir ce tri, et le plan lisait
-- `turn_costs_time_idx` jusqu'à trouver une ligne de l'agent — ~6 800 lignes
-- et 2,2 Go de buffers par appel sur 41 000 tours. Le même balayage, répété
-- par chaque exécuteur, sature la base et fait répondre `/api/v1/alerts` en
-- 90 s. Cet index-ci sert exactement l'égalité et l'ordre du `LIMIT 1`.
-- Additive seulement : aucun index existant n'est retiré, aucune donnée ne
-- change.
set local lock_timeout = '5s';

create index if not exists turn_costs_agent_recorded_idx
    on turn_costs (agent, recorded_at desc, id desc);
