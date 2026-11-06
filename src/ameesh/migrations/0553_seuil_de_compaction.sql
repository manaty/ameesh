-- SPDX-License-Identifier: AGPL-3.0-only
-- #6040 (manaty/ameesh#4637) : seuil de compaction native du harnais, par agent.
-- Les longs tours Codex relisent un contexte qui croît à chaque appel d'outil
-- (IMA : ~6,3 M jetons d'entrée par tour) ; le harnais sait compacter en cours
-- de tour (`model_auto_compact_token_limit`), ameesh ne le réglait pas.
-- `compact_tokens` : jetons de contexte au-delà desquels le harnais compacte,
-- passé au lancement par l'option `ameesh.compact` de son descripteur ; NULL =
-- défaut du harnais (rien n'est passé). Pas de 0 : un seuil nul ferait
-- compacter à chaque appel ; plancher 10 000.
alter table agent_registry
    add column if not exists compact_tokens bigint
        check (compact_tokens is null or compact_tokens >= 10000);
