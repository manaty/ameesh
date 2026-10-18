# Paquet integration/paquet-h-20261018-0500 — composition

Base : `main` **`44cb7dc51840`** (fusion de la PR #1697, paquet h-2300, 02:06Z). Aucun paquet n'était ouvert ; `main` n'a pas bougé depuis cette fusion pendant la composition.

Demandes : **bureau** — mail du 18/10 02:48Z : main libre, aucun paquet ouvert, composer le suivant (§11 bis) ; priorité #2296 (P1, alerte SLO), prêts d'après leurs auteurs #2285 (`352ba651`, résolution `1c3a69d9`), #2340 (`e5b037d9`), #1844 (`9ff7c4832`), #2333 (`511c43346`, avis dev1 en attente). **Orchestrateur** — mail du 18/10 02:44Z : #2289 prêt (`84045d1a`, avis codex3 + mty-nxl-rel-01), avec #2341 (`466609e44`), #2342 (`c40fde773`), #2336 (`e9ee6759e`) ; #1515 à ne pas inclure ; puis 02:57Z : #2338 prêt (`2ed2eba38316`, avis cx3). **mesh-design (pilote)**, messages de 02:52/02:53 signés « bureau » par erreur, identité corrigée à 02:57Z : ajouter #1945 (`b7d9bbcc1430`), #2328 (`60f6d19f90f7`), #2335 (`2ec1b266ffda`), #2306 (`809823bf3765`), #2171 (`dcadecfb66f5`) ; #2340 porte une ligne de test `SHOW server_version_num` refusée sous psql, corrigée dans #2355 (`97d7b760f`, relecture en cours) : à défaut, reprendre l'adaptation mécanique. Le composeur ajoute #2306 (deux avis) car la résolution `1c3a69d9` lie la paire #2306 × #2285, confirmée par le pilote (fil #2285, 02:20Z).

## Admissions (14 lots)

- #2296
- #2285
- #2306
- #2340
- #2289
- #2341
- #2342
- #2336
- #1945
- #2171
- #2328
- #2338
- #2280
- #1658

## Composition (14 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2296 | `12b959de45b3c630c554ccb9d393065bf1b712e7` (`lot/2296-admission-pression-memoire`, PR #1689) | `2ba41f91b05f` | deux avis sur la tête : Claude `mesh-design` + Codex `ameesh-cx1` ; classe prod sensible (`host_*`, `resources.py`, `canon.py`) ; composition propre |
| 2 | #2341 | `466609e44b4a11e2fc0e4a7b552f41dfd9f1da7c` (`auditeur/host-unsampled`) | `5e77d7dce95d` | avis Codex `codex1` sur la tête (auteur Claude : autre famille) ; classe prod sensible (`exploitation.py`, `notify.py`) ; composition propre |
| 3 | #2342 | `c40fde773f01e2c90bd76176e0306e81acc56014` (`auditeur/engagement-overdue-p80`) | `bd3dbcebd03d` | avis Codex `ameesh-cx1` sur la tête (auteur Claude) ; alerte/roadmap (`roadmap.py`, `roadmap_dev.py`) ; composition propre |
| 4 | #2340 | `e5b037d9b3468dd980987b12bf7d64377d626326` (`auditeur/consigne-auditeur-dsn-journal`, PR #1736) ; tête gelée finale `18c62c071` | `b791534bcd56` + adaptation `01435d13a4` + merge `ddf9f6529` | deux avis sur la tête : Claude `ameesh-dev4` + Codex `ameesh-cx1` ; SQL/droits (`deploy/sql/role-auditeur.sql`), contrat `tests/parts/toujours.txt` ; adaptation psql de la ligne `server_version_num` reprise de #2355 (`97d7b760f`), test seul, aucun avis requis ; la tête refreezée `18c62c071` porte le même delta test (deux avis sur cette tête, arbre identique via `ddf9f6529`) |
| 5 | #2289 | `84045d1a354371e975cc584e8cce60dbe6924219` (`lot/2289-required-refusal-slug`, PR #1705) | `3561de311c93` | deux avis sur la tête : Codex `codex3` + DeepSeek `mty-nxl-rel-01` ; porte de fusion (`merge_pr.py`) ; composition propre |
| 6 | #2336 | `e9ee6759e5de96e3f40adfa8bc82169304b79f1f` (`auditeur/plafond-contexte`, PR #1737) | `37298ecc7d16` | avis Codex `codex1` sur la tête (auteur DeepSeek : autre famille) ; §8 exécuteur (`runner.py`, `adapters.py`) ; composition propre |
| 7 | #2306 | `809823bf3765e043667fd7b59942dea012f6726a` (`lot/2306-check-read-retry`) | `18474b8a8d01` | deux avis sur la tête : Codex `ameesh-cx1` + Claude `auditeur` ; porte de fusion (`fire.py`, `land.py`) ; composition propre |
| 8 | #2285 | `352ba651d71354dcfc331a11d7130ccd0ff0fe17` (`lot/2285-feu-checks-requis`, PR #1680), via la résolution `1c3a69d9a000792c67021fbf8a91ff120cc80df1` (#2306 × #2285) | `d97951ecb72f` | deux avis sur la tête : Claude `ameesh-dev4` + Codex `ameesh-cx1` ; porte de fusion (`fire.py`) ; résolution refaite sur `352ba651`, `fire.py` de la résolution = tête, adaptation `test_2273` portée par #2285, 314 tests ciblés verts côté dev3 ; composition propre |
| 9 | #1945 | `b7d9bbcc1430516b74d7f166a28c67e602342952` (`lot/1945-attente-atterrissage`) | `2e26a717c936` | avis Claude `ameesh-dev7` sur la tête (autre famille) ; alerte SLO de l'attente d'atterrissage (`stagnation.py`, `work_dependencies.py`) ; composition propre |
| 10 | #2171 | `dcadecfb66f52ec1ce27c5717f239971b87135ce` (`lot/2171-bornes-atterrissage`), via la résolution `97eabb07580f` (#1945 × #2171) | `3adde0b7f617` | deux avis sur la tête : Codex `ameesh-cx1` + Claude `mesh-design` ; bornes de l'attente (`work_dependencies.py`, `test_l368_planned_waits.py`) ; résolution préparée sur les deux têtes gelées ; composition propre |
| 11 | #2328 | `60f6d19f90f75c9893d7bd57737ff42d65eae975` (`lot/2328-ci-target-base`), via la résolution `bf692dcf5017` (#2328 × #2305) | `5b380dd59085` | deux avis sur la tête : Claude `ameesh-dev4` + DeepSeek `mty-amh-impl-07` ; CI de lot (`pulls.base.sha`, `test_l161g_ci_gel.py`) ; résolution préparée contre le paquet h-2300, remise sur la base 44cb7dc51 ; composition propre |
| 12 | #2338 | `2ed2eba38316` (`auditeur/paquet-admission-familles`, PR #1735) | `5954a7da206c` | avis Codex `ameesh-cx3` sur la tête (auteur `mty-amh-impl-08` : autre famille) ; admission des familles (`review_gate`, `merge_queue`) ; composition propre |
| 13 | #2280 | `d6844860c9ab` (`lot/2280-cgroup-doc-modules`) | `ab4847d30694` | deux avis sur la tête : Claude `auditeur` + Codex `ameesh-cx3` ; documentation des modules (`docs/`) ; composition propre |
| 14 | #1658 | `00e9df261698` (`lot/1658-veille-paquets-horaires`, PR #1717) | `c46511d0ad28` | avis Claude `auditeur` sur la tête (auteur `mty-amh-docs-01` : autre famille) ; veille et doc des paquets horaires ; composition propre |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot et il porte le trailer `ameesh-work: #<lot>` ; l'adaptation `01435d13a4` est un commit simple, sans trailer. Les 14 unités et l'adaptation ajoutent **66 fichiers, +3701/−356** vs `44cb7dc51`, dont la SQL de rôle `deploy/sql/role-auditeur.sql` (unité 4, deux familles) et `tests/parts/toujours.txt`, aucun `.github/**`, aucune migration.

## Conflits et résolution

Aucun conflit non résolu entre les 14 unités : les merges `--no-ff` passent rc=0, sauf les trois paires couvertes par une résolution préparée :

- #2306 × #2285 sur `src/ameesh/fire.py` : résolution `1c3a69d9a000` (parents `809823bf` × `352ba651`), utilisée comme source de l'unité 8 ; `git diff 1c3a69d9 <tête> -- src/ameesh/fire.py` vide.
- #1945 × #2171 sur `src/ameesh/work_dependencies.py` et `tests/test_l368_planned_waits.py` : résolution `97eabb07580f`, source de l'unité 10.
- #2328 × #2305 sur `tests/test_l161g_ci_gel.py` : résolution `bf692dcf5017`, source de l'unité 11 ; #2305 est dans la base (h-2300).
- Le contrôle d'inclusion signale #2306 « partiel » sur `src/ameesh/fire.py` (60/70 lignes) : les trois lignes absentes sont des **commentaires** de la version #2306, remplacés par la rédaction relue de #2285 ; le code de reprise #2306 (`_transient_read`, codes 408/429/5xx, `land.TransientLandError`) est présent. #2340 « partiel » sur `tests/test_2340_auditeur_role.py` : la ligne absente est exactement celle remplacée par l'adaptation psql de #2355. #1945 « partiel » sur `stagnation.py`, `work_dependencies.py` et deux tests : les lignes absentes sont celles que #2171 modifie (bornes de l'attente) ; la déclaration d'atterrissage reste présente à la tête (`work_dependencies.land`, `set_landing`, lecture de `landing_sha`). Les trois écarts sont lus et acceptés.

## Adaptation hors unité

#2340 : la ligne de test `SHOW server_version_num` via `db.query` est refusée sous psql ; l'adaptation mécanique de #2355 (`97d7b760f`, `SELECT current_setting('server_version_num') AS v`) est reprise seule dans `01435d13a4`, sans le reste de #2355 (relu à part). Adaptation de test, aucun avis requis (§11 bis) ; le reste de #2340 est celui de sa tête gelée.

Complément (03:07Z) : après la composition, l'auteur de #2340 a refreezé `18c62c071` (= `e5b037d9b` + ce même delta test, contenu de prod inchangé) et obtenu les deux avis sur cette tête (dev4, cx1). Le merge `ddf9f6529` ajoute `18c62c071` comme second parent de l'unité 4 ; son arbre est identique à `01435d13a4` (`git diff` vide), donc la tête gelée du lot est un ancêtre du paquet et le contenu relu est celui qui part en CI.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2335 | demandé par le pilote (02:53Z), mais conflit avec #1945/#2171 sur `src/ameesh/planned_waits.py`, `src/ameesh/stagnation.py` et `src/ameesh/work_dependencies.py` ; aucune résolution préparée (union des deux attentes) → reporté, résolution à préparer |
| #1844 | lot **nexlink** (Pages staging), pas du dépôt `manaty/ameesh` ; branche et commit `9ff7c4832` absents de la forge ameesh |
| #2333 | second avis dû (dev1) sur la tête `511c43346` |
| #2337 | second avis dû (dev2) : classe critique, deux avis requis |
| #2349 | verdict dû (dev4) |
| #711 | conditions du 17/10 18:30 non réunies sur la tête `50fe8357` (avis cx2 #423 historique + second avis SQL) ; migration SQL |
| #1022, #775, #1406 | chaîne de dépendances : #1022 et #775 dépendent de #711 (qa), #1406 dépend de #775 ; hors admission |
| #1515 | consigne du pilote (02:44Z) : ne pas inclure |
| #2212, #897, #1280, #2265, #2263 | verdicts incomplets, tenus par le pilote ou hors admission |
| autres lots qa | verdicts incomplets ou hors admission du pilote |

## Preuves

- `git diff --check 44cb7dc51..HEAD` : rc 0, aucun marqueur de conflit.
- Seconds parents et trailers `ameesh-work` : 14/14 vérifiés sur la première ligne de l'historique ; adaptation `01435d13a4` sans trailer, documentée ci-dessus ; le merge `ddf9f6529` porte #2340 en second parent (`18c62c071`) et le trailer `ameesh-work: #2340`.
- `git diff 1c3a69d9 <tête> -- src/ameesh/fire.py` : vide ; résolutions #1945 × #2171 et #2328 × #2305 utilisées comme sources des unités 10 et 11.
- Chaque tête source est la tête distante gelée (`ls-remote` = sources listées) ; aucun rebase de lot, aucune CI de lot.
- `scripts/paquet-inclusion.py --manifest <ce fichier> --head HEAD --expected 2296,2285,2306,2340,2289,2341,2342,2336,1945,2171,2328,2338,2280,1658` : rejoué sur la tête finale après le commit de manifeste ; 11 lots présents, #1945, #2306 et #2340 partiels sur les écarts lus ci-dessus, aucun défaut.

## CI et fusion

- **Une seule** suite complète sur la tête exacte du paquet : la branche `integration/paquet-h-20261018-0500` est poussée et la PR #1746 ouverte ; l'étiquette `ci-full-<sha>` déclenche l'unique suite `tests.yml`. Aucune CI de lot, aucun rebase.
- Le portier `paquet.yml` du premier push avait appelé une suite avant que la PR soit visible : run `39091876659` annulé (check non nommé `tests`), remplacé par la suite `pull_request` de la PR.
- Le check requis `tests` doit être `success` avec des tests réellement exécutés (pas `skipped`/absent, #2271).
- `main` est réservée pendant la CI (§12) : aucune autre fusion n'y entre. Si `main` bouge sans conflit, la branche est remise à jour, le manifeste réécrit et la tête gelée de nouveau ; l'ancien run est annulé, une seule suite effective sur la tête finale.
- Fusion dès le vert par le composeur (`ameesh work merge-pr` sur la tête gelée, auto-merge GitHub désarmé), puis rattachement des unités au merge réel et clôture. Aucune opération hôte ici.
