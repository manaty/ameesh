# Paquet integration/paquet-h-20261025-14 — composition (§12)

Base : `main` **`52c74fd1c7f612fee38b21b2275cf2d639070410`** (fusion du paquet
h-13, PR #2606, 25/10 00:05Z).
Branche `integration/paquet-h-20261025-14`, cible `main`. Porteur : **#3431**
(créé par mesh-design, fil du lot, 25/10 00:05Z).
Composeur : mty-amh-impl-01. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message **#135329** (25/10 00:05Z) : « h-14 est à toi.
  Déclencheur : la fusion de h-13 (#3420, impl-05, tête 9997633a) […]. Ensuite,
  paquet-candidats.py sur la nouvelle tête de main, puis
  integration/paquet-h-20261025-14 avec tout ce qui est prêt. Déjà annoncés :
  #3400 (267f91c727a2, 2 avis), #3415 (1656510cc608, 2 avis), et #3416
  (qa-01, 94b7be779d79) si ses avis sont acquis. Les fusions doivent être
  identiques au merge-tree, avec le manifeste seul en propre. Après ta poussée,
  fais le git fetch du clone radar ameesh-packets (le correctif #3416 n'est pas
  encore déployé). Annonce-moi le gel. »
- mesh-design, message du 25/10 00:07Z : h-13 est entré dans `main`
  (52c74fd1c, 00:05Z) sans suite complète, sa ci-full ayant été annulée
  (incident consigné sur #3420) ; la suite de `main` tourne (run 40145345413) ;
  « Compose h-14 sur 52c74fd1c comme prévu : sa ci-full couvrira aussi le
  contenu de h-13. […] Si elle échoue sur un test venu de h-13, dis-le-moi
  avant d'écarter un lot de h-14. »
- mesh-design, message du 25/10 00:27Z : candidats à confirmer par
  `paquet-candidats.py` sur 52c74fd1c — #3015, #3407, #3415, #3400, #3424,
  #3435, #3426 (si gelé), #3416 (si ses 2 avis sont acquis), #3429 ; « Ce n'est
  pas le paquet recomposé par impl-05 (b076481df, basé sur h-12, à ignorer) » ;
  « Si la liste dépasse une dizaine de lots, garde les plus anciens et les P1 ;
  le reste ira en h-15. »
- mesh-design, message du 25/10 00:31Z : #3015 a une nouvelle tête gelée
  `db9cceef585c` (avis cx1, documentaire) qui remplace `170deada` ; #3433
  (role canary, `212a098edf63`, avis cx1) à prendre s'il est prêt au gel ;
  « #3015 et #3433 vont ensemble : le second passage EP7 (#3439) les attend
  tous les deux au staging. »
- mesh-design, message du 25/10 00:49Z : ajout de **#3440** (P1, doc seule,
  `docs/EXPLOITATION.md`, gel `e808be66f931`, avis auditeur, PR #2616) « tant
  que la branche n'est pas poussée » ; « Si la branche est déjà composée quand
  tu lis ceci, ne recompose pas : il ira dans h-15. » La branche était déjà
  composée à la réception : #3440 n'est pas recomposé, il ira en h-15.
- Calcul `scripts/paquet-candidats.py --repo . --target main` sur la base
  ci-dessus, 25/10 00:42Z (clone radar) : **10 lots prêts**, 4 en attente, 0
  erreur. Prêts : #3015 (`db9cceef585c`), #3400 (`267f91c727a2`), #3407
  (`1a52c786987c`), #3415 (`1656510cc608`), #3416 (`7e062a3f6b61`), #3424
  (`9e6123bb09e7`), #3426 (`65c52a2823fc`), #3429 (`9fcc27c34229`), #3433
  (`212a098edf63`), #3435 (`0de993a9c34b`). En attente : #3385, #3388 et
  #3422 (`branch-not-published`) ; #3437 (`freeze-not-head` : gel
  `8a3253b51323`, tête publiée `df16a441b88f`).
  #3416 n'était pas prêt au premier calcul (tête `94b7be779d79` sous verdict
  bloqué, sans gel sur `7e062a3f`) et l'est devenu après son gel #18270 et ses
  deux avis ; #3426 est passé en `qa` à 00:34Z (gel 18286, avis cx1 #18289 et
  auditeur #18290) et le calcul final le rend prêt — composé, comme les autres.

## Admissions (10 lots, 10 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3015 | `db9cceef585c` | light | 1/1 : ameesh-cx1 (codex), verdict 18297 | **EP7** : doc de l'exercice en staging — arrêt planifié du poste et coupure réseau, nouveau pilote en moins de 5 min, jamais deux détenteurs, retour du poste en attente |
| #3400 | `267f91c727a2` | sensitive | 2/2 : ameesh-cx2 (codex), verdict 18250 ; auditeur (claude), verdict 18251 | Porte de fusion : `accepted` ne liste plus les `ok` non filtrés par l'indépendance ; ils restent lisibles sous `accepted_unfiltered` |
| #3407 | `1a52c786987c` | light | 1/1 : ameesh-cx1 (codex), verdict 18231 | packet-watch : la composition titre-seul est bornée au créneau et replie sur `target` ; un `QueueError` ne fait plus tomber la veille |
| #3415 | `1656510cc608` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 18236 ; auditeur (claude), verdict 18237 | Désignation des relecteurs : un agent `dead` ou sous pause opérateur n'est plus désignable ni laissé en siège mort |
| #3416 | `7e062a3f6b61` | sensitive | 2/2 : ameesh-cx2 (codex), verdict 18271 ; auditeur (claude), verdict 18272 | Auto-merge de paquet : la lecture de l'historique rapatrie la tête du véhicule ; le correctif de relecteur avertit quand plus assez de familles restent éligibles |
| #3424 | `9e6123bb09e7` | light | 1/1 : ameesh-cx1 (codex), verdict 18248 | SLO `signal_assigned` : un lot créé assigné puis rendu à la file garde sa première attribution ; plus d'alerte de niveau 3 à tort |
| #3426 | `65c52a2823fc` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 18289 ; auditeur (claude), verdict 18290 | Relecture après fusion : le rattrapage ne tente un lot que sur un hôte capable de nommer un dépôt ; les sections critiques lisent le magasin de la transaction |
| #3429 | `9fcc27c34229` | light | 1/1 : auditeur (claude), verdict 18255 | Doc : découpage du câblage de l'adaptateur d'hôte réel du handoff, en lots ordonnés (aucune implémentation) |
| #3433 | `212a098edf63` | light | 1/1 : ameesh-cx1 (codex), verdict 18300 | EP7 P3 : canari de rôle dédié — geste no-op journalisé, époque périmée refusée, preuve négative et positive |
| #3435 | `0de993a9c34b` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 18276 ; auditeur (claude), verdict 18277 | `fire.required_checks` : un dépôt sans GitHub Pro (403 de plan) avec `required_checks: []` vaut « aucune protection de forge », sans retirer la preuve CI d'ameesh |

Les têtes composées sont les têtes **publiées** lues par le calcul (gel effectif
= tête publiée, verdicts acquis sur cette tête). Aucune migration : aucun
`*.sql`, `deploy/` ni `.github/workflows/` dans le diff (vérifié).

## Composition (10 unités, 10 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3015 | `db9cceef585c` | `b5edb2e43e22` | 0 conflit |
| 2 | #3400 | `267f91c727a2` | `796c8f209c61` | 0 conflit |
| 3 | #3407 | `1a52c786987c` | `506af7d6fd6f` | 0 conflit |
| 4 | #3415 | `1656510cc608` | `628af039c819` | 0 conflit |
| 5 | #3416 | `7e062a3f6b61` | `bf2cae578bec` | 0 conflit |
| 6 | #3424 | `9e6123bb09e7` | `98a298efe247` | 0 conflit |
| 7 | #3429 | `9fcc27c34229` | `30e6e147eae2` | 0 conflit |
| 8 | #3433 | `212a098edf63` | `11a0bef4a26b` | 0 conflit |
| 9 | #3435 | `0de993a9c34b` | `bf4e00278784` | 0 conflit |
| 10 | #3426 | `65c52a2823fc` | `3a4b7565a953` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête (`vehicule 1400 : #<lot> <tête>`), et il porte un trailer
`ameesh-work:`. La base n'a pas bougé pendant la composition.

## Contrôles

- **Liste calculée** : 10 prêts, **10/10 composés**, 0 écarté du calcul,
  0 erreur ; #3440, prêt après le calcul, écarté sur consigne du pilote.
- **Conflits** : 10/10 unités fusionnées sans intervention ; chaque arbre de
  composition est identique à `git merge-tree --write-tree` de ses parents
  (vérifié commit par commit) ; aucun marqueur de conflit, aucun arbitrage de
  code ; `git diff --check` vert.
- **Tests ciblés de l'arbre intégré** : 169/169 verts en 221 s sur les modules
  des unités #3015/#3400/#3407/#3415/#3424/#3429 (arbre avant l'ajout de
  #3416/#3433/#3435), **180/180 verts en 113 s** sur les modules des unités
  #3416/#3433/#3435 (respectivement `test_3416_review_correction_preview`,
  `test_3416_vehicle_head_fetch` ; `test_l165_ep4_pilot_gesture` ;
  `test_2271_fire_required_checks`, `test_2273_merge_pr_required`), puis
  **#3426** : `test_1104_review_lot_hors_file` a passé 38 tests puis s'est
  bloqué sur le banc local (attente de sous-processus/DB, aucun échec) ; reprise
  non concluante (§3), l'auteur rapporte sur la même tête `CatchUpWitnessTest`
  22/22, 2585, 3275, `l539` 21/21 et contrat 23/23 verts. La suite complète du
  paquet couvre ces modules.
- **Adaptation de test (aucun avis, §11 bis)** : le premier échec de la run
  40148520341 vient de #3416, `RemoteTargetWarningTest.setUp` : `git branch -f
  main` est refusé quand `init.defaultBranch=main` (la branche est extraite dans
  le dépôt du test). Remplacé par `git update-ref refs/heads/main <base>`
  (déplace la ref sans toucher l'arbre de travail) ; témoin rejoué sous
  `init.defaultBranch=main`, module `test_3416_review_correction_preview`
  25/25 vert. Aucun changement de code produit.
- **Adaptation d'aide (aucun avis, §11 bis)** : #3433 — le bloc `role canary`
  de l'aide racine (`src/ameesh/main.py`) passait après `role run`, ce que
  `test_2179_roadmap_cli` refuse (ordre alphabétique attendu) ; déplacé avant
  `role run`. Aide seule, aucun comportement changé ; `test_2179_roadmap_cli`
  et `test_l165_ep4_pilot_gesture` 63/63 verts.
- **Compilation / contrôles statiques** : 6/6 verts (`scripts/freeze-checks.py` :
  garde mécanique #2449, compilation, attribution #1632, lint L63, alert types
  #1060, doc readers L161a).
- **Prévalidation** : indisponible (card-stale, carte de plus de 48 h) —
  non bloquante, motif journalisé au lot et dans la demande de CI.
- **Diff** : 33 fichiers, +2402/−168 (manifeste compris). Aucune migration.
- **Inclusion** : `scripts/paquet-inclusion.py` rc 0 sur la tête (attendus =
  les 10 admis ; prêts = les 10 du calcul ; 0 écarté).
- **Suite complète** : une seule `ci-full` demandée sur la tête finale, en
  mode paquet ; les verdicts unitaires couvrent les contenus, le paquet est
  relu sur sa tête par les familles du porteur (§12).
- **Fusion** : sous garde de tête, dès le vert et les avis du porteur ;
  landings `ameesh work landing <unité> --vehicle 3431` déclarés au gel.

## En attente (calcul, hors paquet)

| lot | motif |
|---|---|
| #3385 | hold pilote (lot canon) ; branche `canon/3385-roadmap-24-10` non publiée sur origin |
| #3388 | hold pilote (lot canon) ; branche `lot/3388-renommage` non publiée sur origin |
| #3422 | `branch-not-published` : branche `lot/3422-bail-elu-180` non publiée sur origin |
| #3437 | `freeze-not-head` : gel `8a3253b51323`, tête publiée `df16a441b88f` — regeler puis relire |

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3440 | P1 arrivé à 00:49Z, après la composition : le pilote a demandé de ne pas recomposer une branche déjà composée ; il ira en h-15. |

Les 10 lots prêts du calcul de 00:42Z sont composés ; #3440, prêt après ce
calcul, est écarté sur consigne du pilote (branche déjà composée, h-15).
