# Paquet integration/paquet-h-20261024-07 — composition (§12)

Base : `main` **`bf1925787dd2`** (fusion du paquet h-06, PR #2493, 09:59Z).
Branche `integration/paquet-h-20261024-07`, cible `main`. Porteur : **#3334**.
Composeur : mty-amh-impl-05. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message du 24/10 10:06Z : h-07 à composer sur `bf1925787dd2`
  (h-06 fusionné), #3195 et #3320 en tête (#3320 dépend de #3195 ; disque
  agents-2 à 92 %), puis #3311, #3312, #3325, #3326, #3329, #3330. Si #3326
  et #3330 entrent en conflit, garder #3326 et écarter #3330 avec un motif.
  #3231 reste retenu jusqu'à la 1.21.0, absente ; #3313 entre au paquet
  suivant, après son regel. Une CI complète puis fusion sous garde de tête ;
  dernier paquet manuel d'ameesh avant le train.
- mty-amh-impl-03, message du 10:16Z : #3288 est devenu prêt pour h-07 (gel
  `ba421769f326`, 2/2, P1, merge-tree propre sur `bf1925787`) ; relancer le
  calcul avant de figer le manifeste pour l'inclure.
- Calcul `scripts/paquet-candidats.py --project ameesh --base bf1925787dd2`
  (24/10 10:09Z puis 10:16Z, code de `main`) : le second calcul donne
  **13 lots prêts**, 4 en attente, 0 erreur. Prêts : #3288, #3311, #3327,
  #3195, #3231, #3312, #3313, #3320, #3325, #3326, #3328, #3329, #3330.
  Onze sont composés ; #3231 et #3313 sont écartés avec motif ci-dessous.
- #3327 était absent de la liste du pilote de 10:06Z (devenu prêt à 10:09Z) ;
  il est composé, car prêt au calcul et sans hold. Sa fusion a conflicté sur
  `deploy/mise-a-jour/poste.sh` et `vm.sh` ; résolution d'union : les preuves
  de venv de #3311/#3320 et les minuteries de rôle de #3327 sont conservées
  (`bash -n` vert).
- #3330 n'a pas conflicté avec #3326 : la règle du pilote (« garder #3326,
  écarter #3330 en cas de conflit ») n'a pas eu à s'appliquer ; les deux sont
  composés.

## Admissions (11 lots, 11 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3195 | `d5e9d646a68b` | sensitive | 2/2 : ameesh-cx1, verdict 17586 ; ameesh-cx2, verdict 17587 | ménage : `--apply` ignore `node_modules/` et `target/` des worktrees inactifs |
| #3320 | `3dd85557c751` | sensitive | 2/2 : ameesh-cx1, verdict 17616 ; auditeur, verdict 17622 | hôtes sans gpg-agent : activation du ménage des sorties de build |
| #3311 | `d055f6010010` | sensitive | 2/2 : ameesh-cx1, verdict 17604 ; auditeur, verdict 17619 | staging hors créneau : preuve du venv réellement exécuté par chaque runner |
| #3312 | `7b86efae3770` | sensitive | 2/2 : ameesh-dev5, verdict 17563 ; ameesh-cx1, verdict 17613 | CI : les runners ne reçoivent plus de signal d'arrêt en plein job |
| #3325 | `1ec41c1e0c06` | sensitive | 1/1 : auditeur, verdict 17602 | runner : session dédiée au lot d'un autre agent par courrier threadé |
| #3326 | `f4afbb3818cc` | sensitive | 2/2 : ameesh-cx1, verdict 17628 ; auditeur, verdict 17629 | balayage : une tête « vue en avance » contre une cible périmée ne déclare plus fusionné |
| #3330 | `85c018021ac2` | sensitive | 2/2 : ameesh-cx1, verdict 17631 ; auditeur, verdict 17632 | reprise des lots clos à tort par « vu-ancestor » |
| #3328 | `c46f15e13f8a` | sensitive | 2/2 : ameesh-cx1, verdict 17642 ; auditeur, verdict 17669 | désignation au gel : un relecteur dont l'hôte est perdu n'est plus désigné |
| #3329 | `12f2ff88bb17` | sensitive | 2/2 : ameesh-cx1, verdict 17641 ; auditeur, verdict 17643 | porte : un verdict enregistré sur un SHA abrégé est résolu pour la demande de CI |
| #3288 | `ba421769f326` | sensitive | 2/2 : ameesh-cx1, verdict 17676 ; auditeur, verdict 17678 | avis bloquant : corrections dues dans le brief et SLO d'escalade |
| #3327 | `e4ad79ce25ab` | sensitive | 2/2 : ameesh-cx1, verdict 17671 ; auditeur, verdict 17672 | observation GitHub : rôle élu, relève en moins de 5 min |

## Composition (11 unités, 11 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3195 | `d5e9d646a68b` | `745261c0a282` | 0 conflit |
| 2 | #3320 | `3dd85557c751` | `62963b486eb9` | 0 conflit |
| 3 | #3311 | `d055f6010010` | `29585eac2982` | 0 conflit |
| 4 | #3312 | `7b86efae3770` | `f9f76f9f6bdd` | 0 conflit |
| 5 | #3325 | `1ec41c1e0c06` | `1e694ce34919` | 0 conflit |
| 6 | #3326 | `f4afbb3818cc` | `a61a67f0e4c7` | 0 conflit |
| 7 | #3330 | `85c018021ac2` | `354a2a3df672` | 0 conflit avec #3326 |
| 8 | #3328 | `c46f15e13f8a` | `e570eacec12c` | 0 conflit |
| 9 | #3329 | `12f2ff88bb17` | `3b9d3c37ccdc` | 0 conflit |
| 10 | #3288 | `ba421769f326` | `acfd30fca430` | 0 conflit |
| 11 | #3327 | `e4ad79ce25ab` | `c127b69eba52` | conflit `deploy/mise-a-jour/poste.sh`, `vm.sh` résolu en union |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête, et il porte un trailer `ameesh-work:`. Aucun commit de `main` n'est
venu pendant la composition (base `bf1925787dd2` figée).

## Contrôles

- **Liste calculée** : 13 prêts au dernier calcul, **11 composés**, 2 écartés
  avec motif (#3231, #3313) ; 4 en attente hors paquet (voir plus bas).
- **Conflits** : 10/11 unités fusionnées sans intervention ; #3327 a conflicté
  sur deux scripts de déploiement, résolus en union des deux fonctionnalités
  (preuves de venv de #3311/#3320 et minuteries de rôle de #3327). Aucun
  marqueur restant ; `bash -n` vert sur les deux scripts ; `git diff --check`
  vert. Le contrôle d'inclusion signale #3311 **partiel** sur ces deux
  scripts : les lignes de commentaire/contrôle remplacées par l'union sont
  attendues, aucune garde fonctionnelle n'est retirée (les deux contrôles,
  venv des exécuteurs et minuteries de rôle, sont conservés).
- **Tests ciblés de l'arbre intégré** : **344/344 verts**
  (`test_3311_preuve_venv`, `test_3312_ci_runner_drain`,
  `test_3320_agent_gpg`, `test_3288_fixes_owed`,
  `test_3327_role_timers`, `test_2674_verdict_head` : 96 ;
  `test_1617_review_designation`, `test_2447_review_designation_activation`,
  `test_l118_assignments`, `test_l230_build_artifacts`,
  `test_l233b_budget_handoff` : 248).
- **Compilation** : `compileall src tests` verte.
- **Contrôles statiques** : 6/6 verts (`scripts/freeze-checks.py`).
- **Diff** : 54 fichiers, +5216/−263 (manifeste et deux correctifs compris ;
  composition seule : 53 fichiers, +5045/−263 ; correctifs : 3 fichiers
  +18/−3 puis 1 fichier +7/−1) ; `git diff --check` vert ; les onze commits de
  composition portent leur trailer `ameesh-work:`.
- **Migrations** : aucune (aucun fichier SQL ni migration).
- **Prévalidation** : demandée au gel ; carte périmée (`card-stale`) attendue,
  non bloquante (§3), motif consigné.
- **Suite complète** : une suite par tête poussée, par la CI de la PR
  (`ci-full-<sha>`) ; aucune CI de lot. Le run `40086912356` de la tête
  `73a0d17bb` est rouge sur l'intégration #3311 × #3327 (section ci-dessous) ;
  la tête corrigée est regelée puis redemandée en `ci-full`, avec `--avis 0`
  (correctif mécanique §2/§11 bis, le manifeste est la revue du paquet), fusion
  automatique réarmée. Le deuxième tour ne change qu'un banc (#1252) : nouvelle
  tête, `ci-full` et avis sur le seul delta de test. Les onze unités gardent
  leurs verdicts acquis sur leurs têtes gelées.
- **Fusion** : sous garde de tête (`--match-head-commit`), dès le vert, par le
  pilote ou le bureau ; tout rouge non infra isole le lot fautif sans relire
  le reste.

## Correction après CI rouge (tête `73a0d17bb`, run `40086912356`)

La suite complète de `73a0d17bb` a rendu six échecs, tous dans les bancs de
déploiement qui exécutent `vm.sh controler` : trois dans `test_1252`
(`test_control_reports_the_installed_code_without_restarting`,
`test_failed_first_install_leaves_a_journal_and_retour_returns_to_the_left_code`,
`test_retour_sends_a_bundle_for_an_ancestor_of_main_missing_on_the_vm`) et
trois dans `test_503` (`test_check_accepts_held_lease_and_is_read_only`,
`test_check_visits_every_registered_agent`,
`test_controler_prouve_le_venv_du_runner_vivant`), parts `psycopg 1/8` et
`psql 10/16`.

Cause d'intégration #3311 × #3327 : la nouvelle étape `role timers --check`
(et `--install` pour `vm.sh agents timers`) appelait le CLI directement sous
`runuser`, sans l'`EnvironmentFile` du service que #3311 fait lire au CLI ;
sur un hôte sain le contrôle échouait donc faute de DSN (« contrôles en
échec »).

Correctif `8eb727eef` (3 fichiers, +18/−3) : les deux appels passent par
`ameesh_agents` (systemd-run + `EnvironmentFile`), comme `doctor` ; les faux
CLI des bancs #1252 et #503 acceptent `role timers` et journalisent l'appel,
sans affaiblir d'assertion. Correctif d'intégration mécanique, sans avis
(§2/§11 bis).

Ciblés verts sur la tête corrigée : `test_1252_vm_agents_install`,
`test_503_agent_host`, `test_3311_preuve_venv`, `test_3327_role_timers`
(144/144).

Deuxième tour (run `40088134512`, tête `36a835e46`) : les six échecs des bancs
#503 sont réparés ; restent six échecs `test_1252`, tous sur la preuve de venv.
Le détail CI montre le python **enfant** du banc qui meurt à l'initialisation
(`init_fs_encoding`, `No module named 'encodings'`) : le lien `bin/python3`
pointait sur `sys.executable` du venv CI, qui, hors de son préfixe, ne retrouve
pas sa bibliothèque ; la preuve lisait alors un processus mort. Correctif
`f38773b2e` (test seul, 1 fichier +7/−1) : le banc spawne
`sys._base_executable` (python de base complet), l'argv[0] reste celui du venv
et la preuve est inchangée. `test_1252` **48/48** verts en local. Le delta étant
uniquement un banc, la nouvelle tête est demandée en `ci-full --avis 0` ; les
deux verdicts de composition sur `36a835e46` restent acquis.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3231 | **prêt au calcul**, mais tenu hors paquet par le hold de mesh-design : témoin EP7, il n'entre qu'après la 1.21.0, absente. Gel `1af34785` et verdict ameesh-dev2 (claude) acquis ; le hold du 24/10 03:18Z tient. |
| #3313 | **prêt au calcul à 10:16Z** (`92842fa2`, 2/2), mais le pilote a dit au 10:06Z qu'il entre « au paquet suivant, après son regel ». Le laisser à h-08 évite de mêler sa vidange du train au dernier paquet manuel ; aucun contenu n'est perdu, il reste prêt. |

## En attente (calcul, hors paquet)

| lot | motif |
|---|---|
| #3022 | verdicts 2/2 acquis sur `b40d6fde` (ameesh-cx1, mty-amh-impl-05 ok le 24/10) ; entre au paquet suivant, conflit `src/ameesh/mechanics.py` à résoudre par son composeur. |
| #3318 | `freeze-after-verdict` : la tête gelée courante doit être refaite. |
| #3324 | `verdicts-missing` : 1 seule famille, ameesh-cx1. |
| #3186 | `freeze-after-verdict` ; conflit `src/ameesh/release_cutoff.py`. |
