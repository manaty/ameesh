-- SPDX-License-Identifier: AGPL-3.0-only
-- #5250 (CTX-06) : la politique de contexte réglable pour UN agent, sans toucher
-- au canon du harnais ni à l'environnement de l'hôte : le pilote reste réversible
-- et le rôle élu garde ses valeurs quel que soit l'hôte qui détient le bail.
-- NULL = la lecture d'avant (canon `session.max_tokens*`, `AMEESH_*_REREAD_BUDGET`).
-- `session_max_tokens` : seuil de rotation S_hist de cet agent ; jamais 0, le
-- canon refuse 0 et la comparaison `>=` ferait tourner au minimum de tours.
-- `*_reread_budget` : 0 = éteint pour cet agent.
-- `context_pilot` : état du pilote (profil initial, valeurs, retour), écrit par
-- `ameesh context pilot` dans la même instruction que les réglages.
alter table agent_registry
    add column session_max_tokens bigint
        check (session_max_tokens is null or session_max_tokens > 0),
    add column turn_reread_budget bigint
        check (turn_reread_budget is null or turn_reread_budget >= 0),
    add column session_reread_budget bigint
        check (session_reread_budget is null or session_reread_budget >= 0),
    add column context_pilot jsonb;
