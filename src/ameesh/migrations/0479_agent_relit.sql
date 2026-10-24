-- SPDX-License-Identifier: AGPL-3.0-only
-- 0479_agent_relit — #3185 : projets relus hors équipe.
--
-- `relit:` d'une fiche Agent/Persona nomme les autres projets dont l'agent
-- peut relire les lots. La désignation des relecteurs
-- (`review_designation.candidates_from_registry`) admet un agent dont
-- l'équipe est le projet du lot OU dont `relit` le contient. Déclaratif,
-- écrit par `canon sync` comme `projects` ; NULL : aucun projet relu.
-- Additive seulement : aucune ligne existante ne change de sens.

alter table agent_registry
    add column if not exists relit text[];

comment on column agent_registry.relit is
    '#3185: other projects whose lots this agent may review (canon `relit:`), beside its own team; NULL = none.';
