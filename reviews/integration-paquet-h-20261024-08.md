# Paquet integration/paquet-h-20261024-08 — composition (§12)

Base : `main` **`f71d2bd478dc`** (fusion du paquet h-07, PR #2513, 14:20Z ;
main précédent `81be88044`, incident #3336).
Branche `integration/paquet-h-20261024-08`, cible `main`. Porteur : **#3340**.
Composeur : mty-amh-impl-05. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message du 24/10 12:12Z : porteur #3340 créé (dépend de #3334),
  composition validée — #3313 `92842fa2`, #3022 `e5cf9770`, #3116 `741284cf` ;
  rien poussé avant la fusion de h-07 ; puis base sur le nouveau main,
  manifeste, une `ci-full`, fusion sous garde de tête, landings
  `ameesh work landing <unité> --vehicle 3340` après gel.
- mesh-design, 12:53Z : #3337 `27459b08` ajouté (avis ok ameesh-cx1 et
  mesh-design, #17720/#17721) ; merge-tree propre avec les autres et main.
- mesh-design, 14:04Z : #3342 `3d7ed6f891e5` ajouté (boîte veille, 2/2 avis).
- mesh-design, 14:15Z : h-08 compte **7 lots** — #3313, #3022, #3116, #3337,
  #3343, #3342, #3345 — composé dès la fusion de h-07.
- Calcul `scripts/paquet-candidats.py --project ameesh --base f71d2bd478dc`
  (24/10 14:2xZ, venv de production) : **10 lots prêts** (#3022, #3116,
  #3231, #3313, #3335, #3337, #3343, #3345, #3324, #3342), 4 en attente
  (#3186, #3318, #3331, #3341), 0 erreur. Sept sont composés ; les trois
  autres restent pour h-09 (voir « Écartés »).

## Admissions (7 lots, 7 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3313 | `92842fa24dda` | sensitive | 2/2 : ameesh-dev3, verdict 17663 ; auditeur, verdict 17664 | train : finir la vidange avant de vérifier la composition suivante |
| #3022 | `e5cf9770755a` | sensitive | 2/2 : ameesh-dev3, verdict 17695 ; ameesh-cx1, verdict 17693 | canon §5 : la constatation des fusions ne ferme pas un lot de porte de production avant sa présence en prod |
| #3116 | `741284cf4133` | sensitive | 2/2 : ameesh-cx2, verdict 17722 ; auditeur, verdict 17723 | provenance : un commit déjà dans la cible n'est ni auteur ni tiers, y compris repris d'un ancien gel |
| #3337 | `27459b08812b` | sensitive | 2/2 : mesh-design, verdict 17721 ; ameesh-cx1, verdict 17720 | présence : le dernier message déposé est un signe de vie |
| #3343 | `93a25925a490` | sensitive | 2/2 : ameesh-cx1, verdict 17745 ; auditeur, verdict 17744 | CI : carte de mesure réactivée et repli de lot sur les parts de main |
| #3342 | `3d7ed6f891e5` | sensitive | 2/2 : ameesh-cx1, verdict 17730 ; auditeur, verdict 17729 | boîte « veille » : les réponses des agents ne s'y perdent plus |
| #3345 | `2a6f56d88b48` | sensitive | 2/2 : ameesh-cx1, verdict 17762 ; auditeur, verdict 17761 | profils de runner : la divergence avec `config.json` est signalée |

## Composition (7 unités, 7 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3313 | `92842fa24dda` | `3e91725e5` | 0 conflit |
| 2 | #3022 | `e5cf9770755a` | `508d6f451` | 0 conflit |
| 3 | #3116 | `741284cf4133` | `7c8dce716` | 0 conflit |
| 4 | #3337 | `27459b08812b` | `aa0f7bb27` | 0 conflit |
| 5 | #3343 | `93a25925a490` | `52a816fe5` | 0 conflit |
| 6 | #3342 | `3d7ed6f891e5` | `a86ce99f1` | 0 conflit |
| 7 | #3345 | `2a6f56d88b48` | `b461d0f15` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête, et il porte un trailer `ameesh-work:`. Aucun commit de `main` n'est
venu pendant la composition (base `f71d2bd478dc` figée). Une préparation
locale non poussée avait d'abord été bâtie sur l'arbre h-07 (`d2dea851f`) ;
la composition retenue part directement de `f71d2bd`, arbre **identique** au
calcul provisoire (diff vide) — c'est celle-ci qui est poussée.

## Contrôles

- **Liste calculée** : 10 prêts au calcul, **7 composés**, 3 écartés motivés
  (ci-dessous) ; 4 en attente hors paquet.
- **Conflits** : 7/7 unités fusionnées sans intervention ; aucun marqueur
  restant ; `git diff --check` vert. Les merge-trees pairwise des unités et de
  chaque unité sur l'arbre h-07 (`b7851c78`) étaient propres (rc 0) avant la
  composition.
- **Tests ciblés** : **443/443 verts** (2 sautés) en 7 min 28 s sur l'arbre
  composé, sans CI : `test_1003_merge_pr`, `test_1467_train_cutoffs`,
  `test_3022_prod_gate`, `test_3116_provenance_target`,
  `test_3337_presence_hors_executeur`, `test_3343_carte_mesure`,
  `test_3345_profils_runner`, `test_l121_watch`, `test_l161f_plan`,
  `test_l317_project_modules`, `test_undeliverable`, plus le contrat
  `test_2446_train_contract` et `test_3098_train_contract_pins`.
- **Compilation** : `compileall src tests` verte.
- **Contrôles statiques** : 6/6 verts (`scripts/freeze-checks.py`).
- **Diff** : 37 fichiers, +2230/−110 (manifeste compris) ; les sept commits de
  composition portent leur trailer `ameesh-work:`.
- **Migrations** : aucune (aucun fichier SQL ni migration).
- **Prévalidation** : demandée au gel ; carte périmée (`card-stale`) attendue,
  non bloquante (§3), motif consigné.
- **Suite complète** : une seule `ci-full` sur la tête finale, en mode paquet
  (16 parts psql) ; demande `--avis 0 --full` (le manifeste tient lieu de revue
  unitaire du paquet, §12), puis avis delta des deux relecteurs désignés au gel sur la tête.
- **Fusion** : sous garde de tête (`--match-head-commit`), dès le vert et les
  deux avis ; staging = bureau (passage unique après h-07).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3231 | **prêt** (classe light, 1/1), mais tenu hors paquet par le hold EP7 de mesh-design (24/10 03:18Z) : il n'entre qu'après la 1.21.0, absente. |
| #3335 | **prêt et merge-tree propre**, mais absent de la liste h-08 du pilote (14:15Z) ; reste prêt pour h-09. |
| #3324 | **devenu prêt** (2/2, merge-tree propre) après la liste du pilote ; reste pour h-09. |

## En attente (calcul, hors paquet)

| lot | motif |
|---|---|
| #3186 | conflit `src/ameesh/release_cutoff.py` sur la base `f71d2bd`. |
| #3318 | 1 seul avis (mesh-design, claude) sur le gel `cec551e9` ; 2 requis. |
| #3331 | 1 seul avis (ameesh-cx3, codex) sur le gel `641de8af` ; 2 requis. |
| #3341 | 1 seul avis (ameesh-cx1, codex) sur le gel `af8fde83` ; 2 requis. |
