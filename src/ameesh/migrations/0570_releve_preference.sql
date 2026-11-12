-- SPDX-License-Identifier: AGPL-3.0-only
-- 0570_releve_preference — #7048 (avatar B1, docs/design/5322-avatar-humain.md
-- §3.2) : une relève par préférence déclarée.
--
-- `releve: preference` réutilise la relève croisée d'EP1 (bail, epoch,
-- `agent_candidates`, brief) ; seul le classement change : l'ordre de la
-- fiche Placement, pas la disponibilité sur sept jours. `canon sync` écrit
-- dans `agent_candidates.preference_rank` l'index de l'hôte dans cet ordre
-- (0 = hôte préféré, le poste) ; NULL pour un rôle élu.
--
-- Additive : la contrainte s'élargit, la colonne est nullable ; aucune ligne
-- existante ne change de sens.

alter table agent_registry drop constraint if exists agent_registry_releve_valid;
alter table agent_registry
    add constraint agent_registry_releve_valid
    check (releve in ('hote', 'election', 'preference'));

alter table agent_candidates
    add column if not exists preference_rank integer;

comment on column agent_candidates.preference_rank is
    '#7048: index of the host in the Placement order of a releve: preference agent (0 = preferred); NULL for an elected role.';
