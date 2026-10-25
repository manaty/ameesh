-- SPDX-License-Identifier: AGPL-3.0-only
-- 0484_agent_retire — #3460 (décision #135690 a) : retrait d'un agent.
--
-- `ameesh agent retire` marque la ligne retirée : l'agent n'est plus
-- désignable comme relecteur, réclamable par un exécuteur ni destinataire
-- d'un courrier ; son historique et son courrier restent lisibles. Le retrait
-- ne touche ni `status`, ni `mode`, ni le bail : `ameesh agent restore` efface
-- ces trois colonnes et rend exactement l'état d'avant.
-- Additive seulement : NULL partout, aucune ligne existante ne change de sens.

alter table agent_registry
    add column if not exists retired_at     timestamptz,
    add column if not exists retired_by     text,
    add column if not exists retired_reason text;

comment on column agent_registry.retired_at is
    '#3460: when the agent was retired (ameesh agent retire); NULL = active. A retired agent is never designated, claimed nor mailed.';
comment on column agent_registry.retired_by is
    '#3460: who retired the agent (human, orchestrator, design agent or auditor).';
comment on column agent_registry.retired_reason is
    '#3460: why the agent was retired (the decision or the lot).';
