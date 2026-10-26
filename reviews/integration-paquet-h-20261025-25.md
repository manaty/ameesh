# Paquet integration/paquet-h-20261025-25 — composition (§12), sur main issu de h-24

Base : `main` **`fb8b74038e6c9b7ed1143dc9c1baf0c3a71acec1`** (fusion du paquet
h-24, PR #2802, 26/10 00:10Z). Branche
`integration/paquet-h-20261025-25`, cible `main`. Porteur : **#3627**.
Composeur : mty-amh-impl-01 (deepseek, agents-1).

Déclencheur : CI du paquet h-24 verte et fusionnée (radar 00:10Z) ; consignes
mesh-design des 25/10 22:27Z, 22:39Z (composer à partir de
`paquet-candidats` sur la tête de `main`, pas de la seule liste du pilote),
23:03Z et 23:39Z (résolutions mécaniques #3596/#3557 et #3616/#3581),
23:41Z (ordre des unités), 22:45Z (adaptation `MultiFamilyAuthorTest`
annulée avec #3584), 26/10 00:17Z (#3619 ajouté) puis 00:35Z (#3604 sorti :
regelé sur `497a3aed`, avis codex attendu après 07:27Z ; #3608 entre seul,
la contrainte « #3608 avant #3604 » tombe) et 01:44Z (#3584 retiré : rouge
à lui seul sur `test_1374_fire.CompleteAuthorsTest`, changement de la porte
de fusion (#2681) à relire, pas une adaptation de test du composeur).
Liste calculée `paquet-candidats` sur `fb8b7403` : **18 prêts** ; 15
composés, #3617 écarté (conflit avec #3596), #3604 sorti sur la consigne du
00:35Z et #3584 retiré sur celle du 01:44Z. #3596 et #3602 sont ajoutés sur
consigne pilote du 23:41Z malgré leur conflit mécanique avec #3557, avec la
résolution fournie par mesh-design (23:03Z).
#3519 n'est pas composé : sa lecture de porte est incomplète dans
`paquet-candidats` (provenance d'auteur inconnue sur les commits gelés).

## Admissions (17 lots, 17 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3606 | `43197352d5a0` | 2/2 (auditeur claude, cx3 codex) | Gel : l'épingle du contrat de train est contrôlée au gel || #3544 | `2171bca21263` | 2/2 (dev5 deepseek, cx3 codex) | Panier #3533 : trace d'exclusion et portée de production |
| #3550 | `9db20e699edc` | 2/2 (auditeur claude, cx3 codex) | Panier #3532 : durcissement de la file de feu |
| #3611 | `fe1fff08f2b0` | 2/2 (cx3 codex, docs-01 deepseek) | Feu sur porteur `integration/**` : armer PR et étiquette ci-full |
| #3614 | `f68377a4e1c4` | 2/2 (cx3 codex, docs-01 deepseek) | Panier feu-porteur : non bloquants du feu |
| #3608 | `de07dd1ea221` | 1/1 (dev7 claude ; aussi auditeur et dev4) | test_503 : borner `communicate()` après `killpg` |
| #3575 | `ac6688bd5c56` | 1/1 (dev7 claude) | Panier #3568 : alertes et délégation |
| #3572 | `b322f6646924` | 2/2 (docs-01 deepseek, cx3 codex) | Panier #3554 : canon_lowering |
| #3596 | `abfb4c4c0559` | 2/2 (auditeur claude, cx3 codex) | Désignation partielle : pourvoir les sièges disponibles au gel |
| #3602 | `c24fd0d8e1af` | 2/2 (auditeur claude, cx3 codex) | Désignation : conserver la re-désignation manuelle |
| #3616 | `5b0063dd27e2` | 2/2 (cx3 codex, dev2 claude) | INCIDENT CI main : psql sans cryptography |
| #3581 | `0999140ff273` | 2/2 (dev2 claude, cx3 codex) | CI : node absent du PATH des runners, témoin du pont JS |
| #3562 | `a982425b0ce8` | 2/2 (auditeur claude, cx3 codex) | Porte d'avis : `*` et `*/*` ne sont pas nommants |
| #3607 | `5b44cddc19fb` | 2/2 (auditeur claude, cx3 codex) | Incident CI : pas de doublon pour la même exécution |
| #3592 | `8c24a9b44416` | 2/2 (auditeur claude, cx3 codex) | Panier #3557 : plancher illisible |
| #3593 | `4277ffef9833` | 2/2 (auditeur claude, cx3 codex) | Panier porte de fusion : rouge hérité |
| #3619 | `d4558a5750df` | 1/1 (dev2 claude) | Extra crypto absent des venvs d'hôtes |

## Avis au gel

| lot | gel | verdicts |
|---|---|---|
| #3606 | 19431 | #19432 auditeur (claude) ok, #19499 cx3 (codex) ok |
| #3544 | 19199 | #19203 dev5 (deepseek) ok, #19503 cx3 (codex) ok |
| #3550 | 19352 | #19358 auditeur (claude) ok, #19504 cx3 (codex) ok |
| #3611 | 19484 | #19490 cx3 (codex) ok, #19492 docs-01 (deepseek) ok |
| #3614 | 19497 | #19507 cx3 (codex) ok, #19511 docs-01 (deepseek) ok |
| #3608 | 19455 | #19472 auditeur (claude) ok, #19473 dev4 (claude) ok, #19476 dev7 (claude) ok |
| #3575 | 19453 | #19479 dev7 (claude) ok |
| #3572 | 19410 | #19417 docs-01 (deepseek) ok, #19487 cx3 (codex) ok |
| #3596 | 19385 | #19389 auditeur (claude) ok, #19494 cx3 (codex) ok |
| #3602 | 19411 | #19418 auditeur (claude) ok, #19495 cx3 (codex) ok |
| #3616 | 19520 | #19522 cx3 (codex) ok, #19523 dev2 (claude) ok |
| #3581 | 19469 | #19496 dev2 (claude) ok, #19506 cx3 (codex) ok |
| #3562 | 19383 | #19394 auditeur (claude) ok, #19505 cx3 (codex) ok |
| #3607 | 19433 | #19435 auditeur (claude) ok, #19498 cx3 (codex) ok |
| #3592 | 19450 | #19451 auditeur (claude) ok, #19501 cx3 (codex) ok |
| #3593 | 19448 | #19452 auditeur (claude) ok, #19500 cx3 (codex) ok |
| #3619 | 19535 | #19537 dev2 (claude) ok |

## Composition (17 unités, 17 lots)

| unité | lot | source | commit |
|---|---|---|---|
| 1 | #3606 | `43197352d5a0` | `59e1c5b51` |
| 2 | #3544 | `2171bca21263` | `56d319364` |
| 3 | #3550 | `9db20e699edc` | `cabb693d8` |
| 4 | #3611 | `fe1fff08f2b0` | `3346fed14` |
| 5 | #3614 | `f68377a4e1c4` | `47ec861be` |
| 6 | #3608 | `de07dd1ea221` | `de5912b54` |
| 7 | #3575 | `ac6688bd5c56` | `1d58d18ce` |
| 8 | #3572 | `b322f6646924` | `1fdd7a1c1` |
| 9 | #3596 | `abfb4c4c0559` (résolution #3557) | `be883ab79` |
| 10 | #3602 | `c24fd0d8e1af` | `49eb178a1` |
| 11 | #3616 | `5b0063dd27e2` | `dfc24f16d` |
| 12 | #3581 | `0999140ff273` (résolution #3616) | `d9d45d9b1` |
| 13 | #3562 | `a982425b0ce8` | `7a3450556` |
| 14 | #3607 | `5b44cddc19fb` | `e50332d55` |
| 15 | #3592 | `8c24a9b44416` | `54a77c99a` |
| 16 | #3593 | `4277ffef9833` | `d1f7c12bf` |
| 17 | #3619 | `d4558a5750df` | `86bfd77b7` |

Les unités #3614 (après #3611) et #3602 (après #3596) contiennent
respectivement les têtes de #3611 et #3596 : la composition les reprend dans
cet ordre sans conflit.

## Adaptations mécaniques (§2, aucun avis)

1. **#3596 × #3557** (`src/ameesh/review_designation_activation.py`, dans
   l'unité 9) : constantes `FLOOR_PREFIX` (main) puis `MANUAL_PREFIX` (lot) ;
   sous `if _manual_covers(inputs):`, `_manual_outcome(db, item, freeze,
   inputs.manual)` du lot, puis le bloc `if completion is not None:` de #3557
   conservé tel quel. Résolution fournie par mesh-design (25/10 23:03Z), verte
   en tests ciblés (3602, 3596, 3557, 1617, 2739, 2888, 3542, 2447).
2. **#3616 × #3581** (`tests.yml` et `src/ameesh/train_contracts.py`, dans
   l'unité 12) : une seule ligne d'installation `pyyaml>=6` +
   `cryptography>=41` pour la part psql ; l'épingle du blob de `tests.yml`
   dans `train_contracts.py` est recalculée sur le `tests.yml` final du paquet
   (`c220b4dfccc7f3ebec550d92cc8f951b1703aa3f`, `stale_pins` vide). Le
   correctif node 22 de #3581 (setup-node + témoin, ancres reprises par psql
   et fs-class) est conservé.
3. **#3616 × #3609** (h-24, `tests/test_3616_psql_crypto.py`, commit
   d'adaptation) : `test_le_temoin_nexlink_ne_se_saute_pas` n'exige plus
   l'absence de `skipUnless` dans `test_3536_notify_nexlink.py` — #3609 (h-24)
   y garde un saut **conditionnel** à l'absence de `cryptography` ; le test
   vérifie que le saut reste conditionnel et que la part psql installe
   l'extra (test voisin `test_part_psql_installe_cryptography`).

Le commit d'adaptation porte l'adaptation 3, plus la mention des adaptations
1 et 2 consignées dans leurs unités. Aucun avis demandé (§2).

## Écartés de h-25

| lot | motif |
|---|---|
| #3584 | rouge à lui seul (`test_1374_fire.CompleteAuthorsTest`, refus multi-familles) : changement de la sécurité de la porte (#2681) à relire, retiré sur consigne mesh-design du 26/10 01:44Z ; son adaptation `MultiFamilyAuthorTest` est retirée avec lui |
| #3604 | regelé sur `497a3aed`, avis codex attendu après 07:27Z : sorti de h-25 sur consigne mesh-design du 26/10 00:35Z |
| #3617 | conflit avec #3596 (`src/ameesh/review_designation_activation.py`) : panier de #3596 composé sur `336a64b0`, à recomposer sur `main` après h-25 |
| #3519 | lecture de la porte incomplète : provenance d'auteur inconnue sur les commits gelés (chaîne handoff) — pas complet dans `paquet-candidats` |
| #3621 | aucun verdict sur le gel (`no-verdict`) |
| #3526 | 1 avis sur 2 requis (sensible) et conflit handoff (`handoff_factory.py`, `runner.py`, `test_3472_handoff_runner.py`) |
| #3589 | aucun avis ok sur le gel `1f32cf6fad5a` (sensible) |
| #3598 | aucun avis ok sur le gel `d583fc8e7220` (sensible) |
| #3576 | hold pilote depuis le 25/10 23:42Z (siège codex tenu pour cx1) |
| #3457 | hold pilote depuis le 25/10 07:54Z et branche non publiée |
| #3473 | lecture de la porte incomplète (famille d'auteur ambiguë pour cx2) — chaîne handoff |
| #3474 | lecture de la porte incomplète (provenance d'auteur inconnue) — chaîne handoff |
| #3506 | lecture de la porte incomplète (provenance d'auteur inconnue) — chaîne handoff |
| #3509 | lecture de la porte incomplète (provenance d'auteur inconnue) — chaîne handoff |

Les erreurs de lecture de la chaîne handoff (#3473, #3474, #3506, #3509,
#3519) sont hors h-25 par consigne pilote (déjà hors h-22/h-23/h-24 pour le
même motif) : elles ne rendent pas la liste des lots prêts incomplète pour ce
paquet.

## Contrôles

- Composition : 17 unités sur `fb8b74038e6c9b7ed1143dc9c1baf0c3a71acec1` ;
  deux conflits mécaniques résolus (#3596/#3557, #3616/#3581) et une
  adaptation de test (#3616/#3609), sans avis (§2).
- Liste calculée `paquet-candidats` sur `fb8b7403` : **18 prêts** ; 15
  composés, #3617 écarté avec motif, #3604 sorti sur consigne du 00:35Z et
  #3584 retiré sur consigne du 01:44Z ; #3596 et #3602 ajoutés sur consigne
  pilote ; #3519 non complet (lecture de porte).
- Tests ciblés locaux sur l'arbre intégré : 12 tests
  `test_1374_fire.CompleteAuthorsTest` OK ; 66 tests de désignation
  (1617, 3596, 3602) OK ; 20 tests de contrat et de CI (3562, 3616, 3581,
  3606, 2446) OK ; le témoin #2447
  `test_a_manual_reviewer_with_an_unavailable_policy_alerts_and_says_unknown`
  OK. Le module #2447 complet et les autres modules touchés (3557, 2739,
  2888, 3415, 2924, 2961, incidents 1681/2954, 3592, 3558, 3619, 1252) sont
  couverts par la ci-full unique sur la tête gelée — pas de CI par lot
  (§11 bis).
- Contrôle d'inclusion `scripts/paquet-inclusion.py` sur la tête du
  manifeste : aucun défaut ; 18 prêts, 17 admissions, #3604, #3584 et #3617
  écartés au manifeste ; 14 contenus présents, 3 partiels lus (#3611, #3596,
  #3616), réécritures attendues par les unités suivantes et l'adaptation.
- Une seule suite complète sur la tête gelée, après le gel (`ameesh ci run
  <porteur> --full`), pas de CI par lot (§11 bis).
- Après fusion : `main` reçoit la désignation partielle et la
  re-désignation manuelle (#3596/#3602), la part psql avec cryptography
  (#3616/#3581), la file de feu (#3550/#3611/#3614), et les correctifs de CI
  (#3606, #3607, #3592, #3593, #3619).
