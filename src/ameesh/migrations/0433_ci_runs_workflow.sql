-- SPDX-License-Identifier: AGPL-3.0-only
-- 0433_ci_runs_workflow — #1110 : une exécution suivie porte son workflow.
--
-- Le relevé de CI ne suivait qu'un nom de workflow, `tests.yml`, pour tous les
-- dépôts de `github.projects`. Un dépôt dont les suites portent d'autres noms
-- (nexlink : `backend-suite.yml`, `website-suite.yml`, `scripts-suite.yml`,
-- `sql-invariant.yml`…) n'était donc jamais relevé : ni suivi des exécutions,
-- ni verdict, ni réveil de l'agent en fin de CI.
--
-- Le suivi devient une liste par projet (`github.projects.<projet>.workflows`),
-- découverte par défaut dans le dépôt quand `tests.yml` n'y est pas publié.
-- `ci_runs.workflow` porte le fichier du workflow de chaque exécution : le
-- verdict nomme la suite concernée (un vert de `backend-suite.yml` n'est pas
-- « la CI est verte ») et `ameesh ci status` distingue les workflows d'une
-- même tête. Plusieurs exécutions d'une même tête gardent chacune leur ligne
-- et leur statut.
--
-- Additif : une colonne avec défaut, aucun retrait, aucune contrainte
-- resserrée. Les lignes déjà relevées ne pouvaient être que des exécutions de
-- `tests.yml` (le filtre global d'avant #1110) : elles sont nommées, pour que
-- la référence du rouge hérité (`base_failures`) ne compare jamais deux suites
-- étrangères.

alter table ci_runs
    add column if not exists workflow text not null default '';

update ci_runs set workflow = 'tests.yml' where workflow = '';
