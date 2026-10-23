-- SPDX-License-Identifier: AGPL-3.0-only
-- 0477_election_brief — L165 (EP3) : briefing de reprise d'une relève élue.
--
-- Une réclamation croisée (agent `releve: election` dont la ligne était encore
-- chez un autre hôte) écrit ici, dans la même instruction, le briefing
-- construit par l'hôte qui gagne. Le nouveau détenteur le lit à l'ouverture
-- de sa session neuve : il en fait le résumé de reprise (aucune session ne
-- traverse un hôte), puis la colonne est oubliée avec la session
-- (`clear_session`) ou à l'ouverture de la session (`set_session`).
--
-- Contenu libre (état du mesh : lots, courrier, décisions, alertes) : jamais
-- lisible par le rôle superviseur, comme `pending_prompt` et `restart_brief`.

alter table agent_registry
    add column if not exists election_brief text;

comment on column agent_registry.election_brief is
    'L165 (EP3): resume brief written atomically by a cross-host election claim; read once by the new holder as its new session summary.';
