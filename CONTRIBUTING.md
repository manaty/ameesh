# Contribuer à ameesh

Merci de votre intérêt. Quelques règles avant d'ouvrir une demande de fusion :

- **Licence** : ameesh est publié sous **AGPL-3.0-only** (voir `LICENSE`).
- **Accord de contribution** : les contributions externes ne sont acceptées
  qu'après signature d'un accord de contribution (CLA) qui permet à Manaty de
  continuer à proposer ameesh sous d'autres licences. Le texte de l'accord est
  en préparation : ouvrez d'abord une issue pour discuter de votre proposition.
- **Aucun nom de client** dans le code, les tests, les exemples ou la
  documentation : utilisez des organisations fictives (par exemple « acme »).
- **Revue proportionnée au risque** : SQL, sécurité, autorisation, exécuteur et
  tout ce qui touche à la production sont relus avant fusion ; voir
  `docs/design/decisions/0018-vitesse-des-agents.md`.
- **Tests** : la suite tourne sur un vrai Postgres avec les deux pilotes ;
  `scripts/test.sh` en local (ou `scripts/test-parallele.sh -n 4`, en parts
  parallèles), l'intégration continue sur chaque PR : psycopg complet et psql
  sur les modules de `tests/parts/pilote-psql.txt`, en 3 parts pour rester sous
  le plafond de 30 min des hôtes auto-hébergés mutualisés (#947) ; le psql
  complet tourne sur `main` et `release/*`. Un module de test qui touche la
  couche base ou un comportement propre à un pilote s'ajoute à cette liste. Une PR qui ne change
  **que le numéro de version** (`pyproject.toml` seul, la seule ligne
  `version = "…"`) ne relance pas la suite : `main` l'a déjà validée pour ce
  contenu ; le contrôle « tests » reste vert (`scripts/version-only.py`, L157).
- **PR de documentation seule** (L161a) : si chaque fichier changé est sous
  `docs/` ou `site/`, ou un `*.md` hors de `src/`, `tests/`, `.github/` et
  `examples/`, la CI ne passe que les tests qui lisent la documentation,
  sans base (« lecteurs de la doc », moins d'une minute). Un test qui ouvre
  un fichier de `docs/` ou `site/` se déclare dans
  `tests/parts/lecteurs-doc.txt` ; `tests/test_l161a_ci_sans_code.py` le
  vérifie. Sur `main`, la suite complète tourne à chaque fusion et n'est
  jamais annulée par la fusion suivante.
- **Tests indépendants du poste** (L142) : le poste et la CI donnent le même
  verdict. À l'import, `tests/support.py` retire toutes les variables
  `AMEESH_*`, `AGENT_MESH_*` et `AGENT_MAIL_*` de la session (identité
  d'agent, schéma, état, DSN, canon…), sauf les réglages du banc
  (`AMEESH_TEST_DSN`, `AMEESH_REQUIRE_DB`, `AMEESH_DRIVER`, `AMEESH_DB_TRACE`
  et leurs alias `AGENT_MESH_*`), et pointe `AMEESH_CONFIG` et
  `AGENT_MESH_CONFIG` vers un fichier **vide mais existant** (`{}`) : ni
  `~/.config/ameesh/config.json` ni l'ancienne config agent-mesh ne sont lus,
  et un chemin explicite absent est refusé avant toute connexion (#1127).
  `HOME` reste celui du poste (le banc lit `~/.pgpass`). Les caches de
  gestionnaires de paquets du poste (`npm_config_*`, `pnpm_config_*`,
  `YARN_CACHE_FOLDER`, `XDG_CACHE_HOME`) sont retirés de même (L73) : le
  ménage les redirige et respecte un choix déjà posé, donc la suite ne doit
  pas les hériter. Un test qui a besoin d'une config, d'un canon ou d'une
  variable la pose lui-même ;
  `tests/test_l142_env_isole.py` vérifie qu'un poste « pollué » voit la même
  chose qu'un poste propre. Tout module de test qui touche à l'environnement
  importe donc `tests.support`.
- **Historique : git et les PR** (L160a) : la description de la PR dit ce
  qui change et pourquoi ; aucun journal tenu à la main. Les PR fusionnées
  sont liées depuis `docs/design/index.md`.
- **Ajouter plutôt que modifier** (L160a) : on ajoute des fichiers (un module,
  un test, une page, une décision par sujet) plutôt que de modifier un fichier
  commun, pour que deux PR ouvertes en même temps ne se gênent pas à la fusion.
  Les listes centrales (`tests/parts/pilote-psql.txt`…) ont une entrée par
  ligne, rangée par ordre alphabétique, sa raison en commentaire sur la même
  ligne ; `scripts/test-parts.py` refuse une liste mal rangée.
- **Migrations** (L122) : un numéro de migration se réserve avant d'écrire
  le fichier, avec `ameesh work migration <lot>` (après un `git fetch`) ; le
  fichier s'appelle `src/ameesh/migrations/<numéro réservé>_<nom>.sql`. Une
  contribution sans lot prend le numéro suivant le plus haut de `main` et de
  toutes les PR ouvertes. Deux migrations de même numéro font échouer la
  suite unitaire (`tests/test_l122_numeros_de_migration.py`) : la dernière
  venue se renumérote. Une migration ne fait qu'ajouter, sauf version majeure
  (voir ci-dessous).
- **En-tête de licence** `SPDX-License-Identifier: AGPL-3.0-only` en tête des
  nouveaux fichiers source.

La conception (décisions, exigences, spécification) est dans `docs/design/`.

## Numérotation des versions

Règle du propriétaire (2026-10-11). Le numéro `MAJEUR.MINEUR.CORRECTIF` dit
ce que la version change, pas quand elle sort :

| Numéro | Quand | Déploiement |
|---|---|---|
| **majeur** (2.0.0) | rupture : contrat entre agents changé, commande ou option retirée, migration qui ne fait pas qu'ajouter | proposé au propriétaire |
| **mineur** (1.7.0) | nouvelle fonction, commande, option ou réglage ; migration qui ne fait qu'ajouter | proposé au propriétaire, préparé entièrement |
| **correctif** (1.6.6) | corrections de bugs seulement, sans migration ni commande nouvelle | sans attendre le propriétaire, dès que la CI est verte |

Une version porte le numéro de son changement le plus fort. Chaque version est
étiquetée `v<numéro>` sur le commit de `main` déployé et publiée sur GitHub.
The CI checks the number of every PR that changes it (job `version number`,
required through `tests`; lot L158), and the internal auditor checks each
published version ([AUDITEUR](docs/AUDITEUR.md), check 13). Both run
`scripts/version-level.py`, which compares the number with the content since
the previous `v*` tag. What it sees sets a floor:

- **major**: a migration of an earlier version changed or removed; a new
  migration that drops a table, column, schema, type or sequence, renames,
  changes a column type, adds `NOT NULL`, truncates or deletes rows (dropping
  a view, trigger, index, constraint or function to recreate it is not a
  break); a command or command alias removed; an option removed from a
  command (each command counts on its own); an `AMEESH_*` setting no longer
  read; a commit with the trailer `ameesh-breaking: <why>`. No script sees a
  changed agent contract, so its author writes that trailer, in a commit that
  reaches `main` (PRs are merged, not squashed);
- **minor**: a new migration, command, alias, option of a command or
  `AMEESH_*` setting;
- **patch**: anything else.

The number must be the next one at its level (from 1.9.0: 1.9.1, 1.10.0 or
2.0.0), and that level must not be below the floor. A level above the floor
passes: a new function without a new command or setting is a minor that no
script sees. The release notes then say why.

The script reads the SQL and the syntax tree, it does not run them; it helps
the review without replacing it. Known limits: SQL strings are read with
`standard_conforming_strings` on, the escapes of an `E''` string are not
decoded (`E'\x44ROP'`), dollar tags are ASCII, `U&"…"` identifiers are not
decoded, and a function body is never a statement, even when the migration
runs it later (`SELECT f()`). In the CLI, imports hold for their whole
scope, and a helper rebound by assignment, `global` or `nonlocal`, or called
from a nested function, is not followed. To see the level of what is on
`main` before preparing a version:
`python3 scripts/version-level.py level $(git describe --tags --abbrev=0) origin/main`.
