-- SPDX-License-Identifier: AGPL-3.0-only
-- #7674 (contrat public #7636/E3a): politique de session déclarée par la
-- carte Agent/Persona du canon, consommée par l'exécuteur.
--
-- NULL = aucune politique déclarée : `ameesh set` et les défauts de
-- l'exécuteur restent seuls juges. Objet JSON borné, écrit par
-- `canon sync` seulement depuis une fiche lue :
--   {"session_policy": "par-lot"|"taille"|"jamais",
--    "fresh_session": true,
--    "context_max_tokens": <entier >= 0>,
--    "failure_wait_min": <secondes >= 0>}
alter table agent_registry
    add column if not exists declared_policy jsonb
        check (declared_policy is null or jsonb_typeof(declared_policy) = 'object');

comment on column agent_registry.declared_policy is
    '#7674: per-agent session/fresh/context/retry policy declared by the canon '
    'Agent or Persona card; NULL = nothing declared.';
