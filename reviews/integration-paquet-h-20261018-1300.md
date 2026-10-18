# Paquet integration/paquet-h-20261018-1300 — composition

Base : `main` **`63500a3cd8ee`** (fusion de la PR #1815, paquet h-1200,
18/10 12:34Z, CI 39125424002 verte sur la tête `31c63f0b2` ; puis PR #1821,
correctif incident #2450 test-only, 18/10 12:35Z). L'arbre de base est la tête
h-1200 `31c63f0b2` + `tests/test_runner.py` ; `main` a bougé une fois pendant
la composition, réintégré par le commit de mise à jour sans unité `9ad10e5f2`.

Demandes : **mesh-design** (pilote) 18/10 12:13Z (#423, #2421 ; vérifier
#1819) et impl-03 18/10 12:13Z (#2421 à inscrire au manifeste) ; liste
calculée des lots prêts (#2404). Vérification indépendante des têtes gelées,
des verdicts et des merge-tree : cinq lots composés.

## Admissions (5 lots, 5 unités)

- #423, #2421, #775, #2206, #2298.

## Composition (5 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #423 | `eedef7c51e6a` (résolution) | `e7d0cbab0` | alias d'agents : `agents.resolve_name` et résolution dans les chemins mail/fils/lots/placements/sessions/worktrees/systemd/clés ; remise `refs/ameesh/resolutions/423/main-8493fa3c-dev2` (dev2/Claude, B1 corrigé par lui-même, +5 SQL) ; 43 fichiers +1352/−60, migration `0418_agent_aliases.sql`, contrat 66 opérations ; verdict dev2 (Claude) ok sur `eedef7c5` (12:10) ; GO cx2 (Codex) conservé sur `0eaf1822` (08:44) + jalon delta ok sur `eedef7c5` (12:30) ; sensible identité/SQL |
| 2 | #2421 | `a8110967d90b` (tête gelée = publiée) | `4d834aa23` | usage résident Codex app-server et ACP compté au registre : `TurnResult` → `resident.turn` une fois par tour, `pending_spend.resident_usage` pour la réparation d'un tour interrompu, base USD durable ACP ; 13 fichiers +909/−84, migration additive `0464_spend_pending_resident_usage.sql` ; verdicts cx1 (Codex, 11:40) + dev4 (Claude, 12:12) ok sur `a8110967d`, porte 2/2 ; sensible SQL/stockage/argent |
| 3 | #775 | `1d1047c8a86c` (tête corrigée, rebasée sur main `63500a3c`) | `53981bfa0` | vérificateur de liens/ancres de `docs/` versé dans `scripts/doc-links.py` + `tests/test_doc_links.py` ; 3 fichiers +298 ; verdicts auditeur et bureau ok sur `a02928cf` (14/10), conservés (rebasage mécanique) ; **tête remplacée le 18/10 12:43Z** : ligne `test_doc_links` remise à sa place alphabétique dans `tests/parts/lecteurs-doc.txt`, blobs `doc-links.py` `c37b532a` et `test_doc_links.py` `827d23dc` inchangés ; doc/outillage |
| 4 | #2206 | `ef5688cb4703` (tête gelée) | `de188a421` | `_deferred_turn` mémorise le motif après `_deferral_over()` au changement de motif (N1 #2160) ; 2 fichiers +20, témoin dans `tests/test_l48_fast_failures.py` ; avis dev1 (Claude) ok sur `ef5688cb` ; sensible (`runner.py`) |
| 5 | #2298 | `e1d7648d74fd` (tête gelée) | `2019cefae` | prévalidation : une préemption par la porte (exit 75 + note « stopped on … host stopped\|draining ») donne `gate-preempted`, `no-vm` conservé sinon ; 3 fichiers +62/−5 ; avis cx1 (Codex) ok sur `e1d7648d` (12:15) ; léger |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et il porte un trailer `ameesh-work: <lot>` (5 trailers de composition).

## Correctif CI du 18/10 12:3xZ

La suite ciblée sur l'arbre composé a rougi
`tests.test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue`
(psql) : la migration `0464` de #2421 ajoute `spend_pending.resident_usage`,
colonne ni accordée ni exclue dans le contrat des rôles superviseur. C'est un
état interne borné au tour (payload de réparation d'un worker mort), pas un
état de supervision : classement **fermé** dans `tests/test_cluster.py`
(`FERMEES`, aucun GRANT modifié), sur le modèle du correctif `0450` de h-1100.

- `PYTHONPATH=src python -m unittest tests.test_cluster` : **34 tests OK,
  6 ignorés** (186 s) après le correctif.
- Commit de composition `b6b861de4` (réapplication à l'identique de
  `e6b581939`), base inchangée.

## Recomposition du 18/10 12:44Z (#775)

Signalement de **mty-amh-docs-01** (18/10 12:41Z) : sur la tête `155ae0420`,
`tests.test_l161a_ci_sans_code.ReadersTest.test_liste_rangee_et_connue` était
rouge — l'union #1517 avait rangé `test_doc_links` après `test_l1124` dans
`tests/parts/lecteurs-doc.txt`, alors que `version_only.doc_readers()` garde
l'ordre du fichier et que le témoin exige l'ordre alphabétique. La correction
est faite à la source sur `lot/775-doc-links` (`1d1047c8a`, rebasé sur main
`63500a3cd`, delta identique +298/3 fichiers, blobs du script et du test
inchangés). L'unité #775 a donc été remplacée et le paquet **recomposé** depuis
`4d834aa23` : mise à jour main `9ad10e5f2`, cinq unités (dont #775
`53981bfa0`, #2298 `b1272f391`), correctif `b6b861de4`, puis le présent
manifeste. L'ancienne tête
`155ae0420` et son run de CI en file (`39128173275`) sont abandonnés ; la CI
complète est demandée une seule fois sur la tête du présent manifeste.

Témoins sur l'arbre recomposé : `tests.test_l161a_ci_sans_code` +
`tests.test_doc_links` + `tests.test_l107_executor_contract` — **41 tests OK**
(0,5 s) ; `tests.test_1770_prevalidation` + `tests.test_l48_fast_failures` —
**58 tests OK** (16,5 s) ; `scripts/doc-links.py` — **169 fichiers, 0 lien
cassé**. Le correctif `test_cluster`/`0464` (`b6b861de4`, 34 OK sur l'ancienne
tête) est inchangé et couvre le même arbre.

## Conflits et résolutions

- #423 × #2421 : conflit unique `docs/EXECUTEUR-MEDIEE.md` (compteurs du
  contrat). Résolu mécaniquement sur le contrat composé : **66 opérations
  admises, 393 refusées, interface 459** (66 + 393) ; `tests.test_l107_executor_contract`
  vert (22 tests) sur l'arbre composé. Aucun autre conflit.
- `git merge-tree --write-tree` rc 0 pour chaque source sur la base
  `31c63f0b2` ; la base finale `63500a3c` n'ajoute que `tests/test_runner.py`
  (incident #2450), sans conflit avec les unités. Seules les paires
  #423 × #2421 (docs) et #423 × #2206 (`runner.py`) se croisent, la seconde
  fusionne sans conflit.
- #423 n'est pas composé depuis sa tête de branche `db2ac0df` mais depuis la
  résolution `eedef7c5` (remise sur `main` `8493fa3c`), dont le delta est
  strictement les 43 fichiers du lot.

## Preuves

- `git diff --stat 63500a3c..HEAD` : **57 fichiers hors ce manifeste,
  +2643/−147** (58 fichiers avec le manifeste), dont `src/ameesh/agent_names.py`,
  `src/ameesh/cost.py`, `src/ameesh/runner.py`, `src/ameesh/resident*.py`,
  `src/ameesh/prevalidation.py`, `src/ameesh/storage/**`, les migrations
  `0418_agent_aliases.sql` et `0464_spend_pending_resident_usage.sql`,
  `scripts/doc-links.py`, les docs et 12 fichiers de tests.
- `git log --first-parent 63500a3c..HEAD` : commit de mise à jour `9ad10e5f2`
  (sans trailer), 5 unités, 5 trailers `ameesh-work` ; têtes sources :
  `eedef7c51e6a`, `a8110967d90b`, `1d1047c8a86c`, `ef5688cb4703`,
  `e1d7648d74fd` ; un correctif de composition `b6b861de4` (sans trailer),
  puis le présent manifeste.
- `git diff --check 63500a3c..HEAD` : rc 0, aucun marqueur de conflit.
- Tests ciblés locaux sur l'arbre composé (`ameesh heavy`, `PYTHONPATH=src
  python -m unittest`) : **375 tests, 549 s** pour les quatre premières unités ;
  2 échecs et 2 erreurs, tous expliqués : le vrai échec `test_cluster`/0464 est
  corrigé ci-dessus (34 OK après), `test_l113_work_repo` est l'identité Git
  ambiante de l'hôte (`GIT_AUTHOR_*`, #1801 ; 29 tests OK avec ces variables
  neutralisées), les 2 erreurs sont les erreurs hôte #1801 (gpg-agent) de
  `test_423`. #2298 : `tests.test_1770_prevalidation` **46 tests OK** (0,4 s)
  sur l'arbre composé.
- `scripts/paquet-inclusion.py --manifest reviews/integration-paquet-h-20261018-1300.md
  --head b1272f391 --expected 423,2421,775,2206,2298 --excused 2326,1819
  --no-ready` : rc 0, **5/5 attendus**, 5 compositions (4 têtes présentes,
  #423 partiel), aucun défaut. La liste calculée des prêts au cutoff
  (5 prêts, #2326 écarté consigné) avait été contrôlée à la composition
  initiale ; son fichier n'est pas conservé, d'où le `--no-ready` explicite sur
  la recomposition. #423 ressort **partiel** sur `docs/EXECUTEUR-MEDIEE.md`
  (compteurs 392→393 et 458→459 réécrits par #2421, expliqué ci-dessus).
- Une seule suite complète sur la tête finale du paquet (§11 bis), demandée
  après la recomposition ; le run en file de l'ancienne tête `155ae0420`
  (`39128173275`) a été annulé ; aucune CI de lot entre deux coupures.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #1819 | Prêt (gel `f7705b52`, avis impl-06 ok), mais `git merge-tree` rc 1 sur `src/ameesh/prevalidation.py` contre la base h-1200 (`31c63f0b2`, conflit avec #1820) ; aucune remise `refs/ameesh/resolutions/1819` : remise fraîche + avis delta requis avant composition. |
| #2326 | Gel `fb726181`, merge-tree rc 0, mais aucun verdict (dev2 désigné 11:17) : relecture à rendre avant le prochain paquet. |
| #2450 | Incident `tests.yml` (run 39122680846), correctif test-only `8dcc1e1f` : fusionné directement dans la base par exception incident (PR #1821), pas une unité de paquet. |
| #2394, #2260, #711, #2160, #2344, #2288, #2404, #2432, #2435, #1820, #2397, #2399, #2408, #2370 | Unités du paquet h-1200 (PR #1815) déjà fusionnées dans la base : hors périmètre de h-1300. |

Les autres lots en `qa` ne sont pas prêts au calcul `paquet-candidats`
(verdicts manquants, gel non égal à la tête, conflit ou dépendance bloquante)
et ne sont pas admis.

## Après fusion (staging)

- Déploiement staging dédié au paquet, VM d'agents d'abord, pc-smichea en
  dernier ; **migrations additives 0418 (#423) et 0464 (#2421) appliquées
  avant la relance des exécuteurs**. Le code #423 lit `agents.aliases` : sans
  0418, le registre et la porte de fusion échouent (famille d'auteur inconnue).
- Puis étape 5 de #1780 par un agent DeepSeek (impl-06) : traces
  dormant/réveil et silence/rotation, versions/commandes/statuts, relevé
  `resident.turn` et lignes de grand livre. #2421 n'est « fait » qu'après
  cette preuve.
