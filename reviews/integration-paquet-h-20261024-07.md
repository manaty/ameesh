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
- **Diff** : 53 fichiers, +5045/−263 (manifeste non compris) ;
  `git diff --check` vert ; les onze commits de composition portent leur
  trailer `ameesh-work:`.
- **Migrations** : aucune (aucun fichier SQL ni migration).
- **Prévalidation** : demandée au gel ; carte périmée (`card-stale`) attendue,
  non bloquante (§3), motif consigné.
- **Suite complète** : une seule suite par tête poussée, par la CI de la PR
  (`ci-full-<sha>`) après les avis requis ; aucune CI de lot. Les onze unités
  gardent leurs verdicts acquis sur leurs têtes gelées ; le porteur #3334 est
  gelé sur la tête finale et ses relecteurs rendent leurs avis sur cette tête.
- **Fusion** : sous garde de tête (`--match-head-commit`), dès le vert, par le
  pilote ou le bureau ; tout rouge non infra isole le lot fautif sans relire
  le reste.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3231 | **prêt au calcul**, mais tenu hors paquet par le hold de mesh-design : témoin EP7, il n'entre qu'après la 1.21.0, absente. Gel `1af34785` et verdict ameesh-dev2 (claude) acquis ; le hold du 24/10 03:18Z tient. |
| #3313 | **prêt au calcul à 10:16Z** (`92842fa2`, 2/2), mais le pilote a dit au 10:06Z qu'il entre « au paquet suivant, après son regel ». Le laisser à h-08 évite de mêler sa vidange du train au dernier paquet manuel ; aucun contenu n'est perdu, il reste prêt. |

## En attente (calcul, hors paquet)

| lot | motif |
|---|---|
| #3022 | `verdicts-missing` sur la tête `b40d6fde` (1 famille, ameesh-cx1) ; revue deepseek en cours après h-07 ; conflit `src/ameesh/mechanics.py`. |
| #3318 | `freeze-after-verdict` : la tête gelée courante doit être refaite. |
| #3324 | `verdicts-missing` : 1 seule famille, ameesh-cx1. |
| #3186 | `freeze-after-verdict` ; conflit `src/ameesh/release_cutoff.py`. |
