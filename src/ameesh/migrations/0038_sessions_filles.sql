-- SPDX-License-Identifier: AGPL-3.0-only
-- 0038_sessions_filles — sessions parallèles d'une persona, une par lot (lot
-- L52b, décision 0029, étude « sessions parallèles », voie B).
--
-- Une session parallèle est portée par un agent éphémère « fille »
-- (`<persona>.l<lot>`) : son propre bail, sa propre session, sa propre ligne
-- de registre. `parent_persona` la rattache à sa persona ; l'historique des
-- sessions est tenu sous la persona, `agent` dit quelle ligne du registre
-- portait la session (la persona elle-même, ou une fille).

alter table agent_registry add column if not exists parent_persona text null;

create index if not exists agent_registry_parent_persona_idx
    on agent_registry (parent_persona) where parent_persona is not null;

alter table persona_sessions add column if not exists agent text null;
