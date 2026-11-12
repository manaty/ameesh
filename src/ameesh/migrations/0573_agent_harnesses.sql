-- SPDX-License-Identifier: AGPL-3.0-only
-- 0573_agent_harnesses — #7004 : harnais admis d'une identité.
--
-- `harnesses:` d'une fiche Agent/Persona (L51) nomme l'ensemble fermé des
-- harnais sous lesquels l'agent peut tourner. Une identité `externe` liée
-- session par session (le bureau : codex sur le PC, claude sur BG) a ainsi
-- plusieurs harnais déclarés ; la provenance d'une session liée
-- (`review_provenance._bound_session`) admet le harnais de la liaison s'il
-- est dans cet ensemble. Déclaratif, écrit par `canon sync` comme `relit` ;
-- NULL : aucun ensemble déclaré, seul `harness` vaut.
-- Additive seulement : aucune ligne existante ne change de sens.

alter table agent_registry
    add column if not exists harnesses text[];

comment on column agent_registry.harnesses is
    '#7004: closed set of harnesses the agent may run under (canon `harnesses:`); NULL = only `harness`.';
