# Paquet integration/paquet-h-20261018-1100 — composition

Base : `main` **`2e94b4f90cb2`** (fusion de la PR #1799, paquet h-1000, 18/10 10:26Z ; arbre identique au gel `1bcbd5f9`, CI 39117454930 verte). Aucun paquet ouvert ; `main` n'a pas bougé pendant la composition.

Demandes : **mesh-design** (pilote) 18/10 10:25Z (#2424, #2371, #2303, #2295 ; #2408/#2298/#2433 écartés) puis 10:31Z (#2433 ajouté, à intégrer après #2428, base `2e94b4f9`) ; notes d'impl-05 (#423, remise `c6dea592`), d'impl-08 (#1770, résolution rafraîchie `9496f17e`) et d'impl-04 (#2428). Sept lots composés : **#1770, #2424, #2428, #2433, #2371, #2303, #2295**.

## Admissions (7 lots, 7 unités)

- #1770, #2424, #2428, #2433, #2371, #2303, #2295.

## Composition (7 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #1770 | `9496f17ebcbf` (résolution) | `15883af08` | prévalidation au gel : tests associés au diff par la carte L161e, exécution VM, causes explicites ; résolution `refs/ameesh/resolutions/1770/main` (parents `4801bba3` × `2e94b4f9`, arbre `f21fe57c`) ; verdicts dev1 (Claude) + cx1 (Codex) ok sur la tête gelée, porte 2/2 ; sensible SQL/identité |
| 2 | #2424 | `6919329559d1` (tête gelée) | `101276677` | staging : `sauvegarde_serveur` refuse le 2e dump du même jour (comparaison de noms) ; verdicts impl-02 (DeepSeek) + cx1 (Codex) ok ; sensible `deploy/**` |
| 3 | #2428 | `5bb3a4221146` (tête gelée) | `d6fe54272` | file de fusion : verrou du clone radar, `CloneBusy` non éjecté ; verdicts cx2 (Codex) + dev7 (Claude) ok, vérification signateur `mty-nxl-impl-02` consignée ; sensible (concurrence/file) ; la ligne `out["error"] = busy` de ce commit est retirée par #2433 (N1), composé après — contenu partiel attendu, pas une omission |
| 4 | #2433 | `5a00d141570c` (tête gelée) | `d39aec785` | `merge_queue.run_pass` : une seule écriture de `out[error]` sur `CloneBusy` (N1 #2428) ; empilé sur #2428 (`5bb3a422` + 1 ligne), composé après lui ; verdict cx2 (Codex) ok |
| 5 | #2371 | `06b69af115bb` (tête gelée) | `b4a0afda6` | 3 N de la relecture dev4 : import de `land.py` robuste à `int(env)`, reset de compteur après admission, doc `EXPLOITATION.md` ; verdicts dev4 (Claude) + cx3 (Codex) ok |
| 6 | #2303 | `40300bde70e9` (tête gelée) | `0fffd9e06` | doc L232B : le SHA-256 de `pull` atteste le transport, pas l'authenticité vis-à-vis du jeu de reprise épinglé ; avis dev4 (Claude), doc seule (verdict sans SHA, note `verdict-names-no-commit`) |
| 7 | #2295 | `b3bcc321f7b3` (tête gelée) | `9425853a3` | `train_ci` : repli sans `--slurp` sur gh 2.45 et décodage des pages concaténées ; avis cx2 (Codex), classe light |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot et il porte un trailer `ameesh-work: <lot>` (7 trailers de composition).

## Conflits et résolutions

- Aucun conflit de composition : `git merge-tree --write-tree` rc 0 pour chaque tête sur la base `2e94b4f9` et pour toutes les paires des sept unités.
- #1770 n'est pas composé depuis sa tête gelée mais depuis la résolution `9496f17ebcbf` (parents `4801bba3` × `2e94b4f9`), qui remplace `a2ebdf04` (base h-0900) à arbre identique `f21fe57c` ; delta vs `2e94b4f9` = strictement les 15 fichiers du lot (+2868/−12).
- #2433 est empilé sur #2428 : sa branche contient #2428 + le delta d'une ligne ; composé immédiatement après #2428, sans résolution.

## Preuves

- `git diff --stat 2e94b4f9..HEAD` : **28 fichiers hors ce manifeste, +3483/−46** (29 fichiers avec le manifeste), dont `src/ameesh/prevalidation.py`, `src/ameesh/mechanics.py`, `src/ameesh/land.py`, `src/ameesh/merge_queue.py`, `src/ameesh/resolutions.py`, `src/ameesh/train_ci.py`, `deploy/mise-a-jour/poste.sh`, la migration `0450_prevalidation_reports.sql`, les docs et 9 fichiers de tests.
- `git log --first-parent 2e94b4f9..HEAD` : 7 unités, 7 trailers `ameesh-work` ; têtes sources : `9496f17ebcbf`, `6919329559d1`, `5bb3a4221146`, `5a00d141570c`, `06b69af115bb`, `40300bde70e9`, `b3bcc321f7b3`.
- `git diff --check 2e94b4f9..HEAD` : rc 0, aucun marqueur de conflit.
- Tests ciblés locaux sur l'arbre composé (`PYTHONPATH=src python -m unittest`) : **224 tests OK** (`test_l232b_release_scripts`, `test_2428_radar_clone_lock`, `test_l160e_merge_queue`, `test_l160e2_land`, `test_l160d_resolutions`, `test_2345_land_retry_bound`, `test_1467_train_ci`, 141 s) et **259 tests OK, 2 ignorés** (`test_1770_mechanics_prevalidation`, `test_1770_prevalidation`, `test_1770_prevalidation_cli`, `test_1770_prevalidation_store`, `test_l161f_plan`, 67 s) — 483 tests au total.
- `scripts/paquet-inclusion.py --manifest reviews/integration-paquet-h-20261018-1100.md --head HEAD --expected 1770,2424,2428,2433,2371,2303,2295` : rc 0, 7/7 présents, verdict « aucun défaut » (le partiel `merge_queue.py` de #2428 est expliqué dans sa ligne de composition).
- Une seule suite complète sur la tête gelée du paquet (§11 bis) ; aucune CI de lot entre deux coupures.

## Correctif CI du 18/10 10:5xZ

La suite complète sur `c56db0af4f11` (run 39120293881) a rougi
`tests.test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue`
(psql 3/16 et psycopg 7/8) : la migration `0450_prevalidation_reports` de
#1770 ajoute dix colonnes que le contrat des rôles superviseur ne classait
pas. Correction de composition : `prevalidation_reports` est classée
**fermée** dans `tests/test_cluster.py` (`FERMEES`) — preuve interne lue par
l'exécuteur, pas un état de supervision ; **aucun GRANT modifié**, le script
des rôles est inchangé.

- `PYTHONPATH=src python -m unittest tests.test_cluster` : **34 tests OK, 6
  ignorés** (176 s) sur l'arbre corrigé, dont le témoin de classement.
- nouvelle tête : le commit de ce correctif, base `2e94b4f9` inchangée ; la
  CI complète est redemandée sur cette tête.
- avis du paquet déjà rendus sur `c56db0af` avant ce delta de test : `cx1`
  (Codex) ok et `mesh-design` (Claude) ok, à reposer sur la nouvelle tête ;
  #2433 garde ses deux avis sur `5a00d141` (`cx2` + `mesh-design`).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #423 | Remise `c6dea592` sur `2e94b4f9` prête (impl-05, arbre `7c2f5f77`, merge-tree rc 0), mais sensible identité/SQL : un seul avis (delta cx2) sur la remise, second avis de famille à poser ; conflit `docs/EXECUTEUR-MEDIEE.md` avec #1770 (compteurs) à résoudre. |
| #2408 | Empilé sur #2371 ; à composer après #2371 (§11 bis). |
| #2298 | Empilé sur #1770 ; à composer après #1770. |
| #2432 | Avis dev4 (Claude) compté sur `c74bf0668` (`on_freeze=1`, branche `origin`, 10:36Z) ; classe sensible tant que #2428 n'est pas dans `main`, puis delta test seul → léger. À admettre en h-1200. |
| #2370 | Sensible : 1 avis sur 2 sur `7ae5343c` (`fire.ready_verdicts` refuse). |
| #2395 | Sensible : 1 avis sur 2 sur `b861fba4`. |
| #2426 | Verdict dev7 en attente sur `f1625fe1`. |
| #2404 | Avis cx2 (Codex) ok sur `3fb8413d` (10:39Z, 0 B) ; merge-tree rc 0 sur `main` `2e94b4f9` et sur la tête h-1100 ; à admettre en h-1200. |
| #2421 | Relecture sur `8d679cdf` après correction B1/B2. |
| #2389 | Verdict dev4 en attente sur `cad4287c`. |
