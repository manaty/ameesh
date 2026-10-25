# Paquet integration/paquet-h-20261025-16 — composition (§12)

Base : `main` **`cc93c592777cf001faf2b3bace2e454ec8fd1c10`** (fusion du paquet
h-15, PR #2652, 25/10 04:40:14Z, auto-merge sur CI verte).
Branche `integration/paquet-h-20261025-16`, cible `main`. Porteur : **#3493**
(créé par mesh-design, 25/10 04:44Z).
Composeur : mty-amh-impl-01. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, 25/10 04:44Z : lot #3493 créé pour le paquet h-16 ; composer sur
  la tête de `main` issue de h-15 (`cc93c5927`).
- mesh-design, 25/10 04:53Z : #3480 prêt (`efe8e1278b`, OK cx3 et dev7) et
  #3464 prêt (`ed1789de`, OK dev1 reporté) ; les runners de pc-smichea sont en
  pause (dev1, dev2, dev4, dev7, cx3, codex2) — aucune adaptation à en
  attendre ; #3447 n'est pas prêt (cx3 a bloqué le dernier gel).
- auditeur, 25/10 04:46Z : inclure **#3454** (`7b8c4737`, relu par deux
  familles, retenu par le pilote à 03:39Z ; retard SLO de niveau 3 sur
  l'étape GO → fusion).
- mesh-design, 25/10 04:57Z : **#3450** prêt (`52aa09fd9`, OK de dev4 et de
  cx2) ; #3447 attend encore cx2 et l'auditeur sur `6f08ac6b` ; si un lot
  porteur de migration additive est emmené, la nommer au manifeste pour le
  staging. #3447 n'est pas du véhicule ; la migration additive du paquet est
  celle de #3451 (0483).
- mesh-design, 25/10 05:10Z : **#3447** est prêt (`6f08ac6b0a01`, OK de cx2 et
  de l'auditeur sur cette tête) ; il porte la migration additive **0482**,
  nommée ci-dessous pour le staging. Recalcul de contrôle à 05:12Z :
  11 lots prêts, #3447 ajouté au véhicule.
- Fil du porteur h-15 (#3461), 03:54Z / 04:15Z / 04:20Z : candidats confirmés
  #3453, #3454, #3467 (puis #3490 juste après, empilé), #3483 ; #3487 si prêt ;
  #3450 écarté à l'époque (re-pin du contrat #2446 en cours, rebase après
  h-15), devenu prêt depuis (`52aa09fd948b`, re-pin `2c2ac22c9` du blob
  `scripts/test-plan.py`, deux avis sur la nouvelle tête).
- Calcul `scripts/paquet-candidats.py` du 25/10 04:57Z sur la base ci-dessus :
  **10 lots prêts**, 7 en attente, 0 erreur. Prêts : #3450
  (`52aa09fd948b`), #3451 (`da667e031d12`), #3454 (`7b8c47372968`), #3480
  (`efe8e1278b92`), #3483 (`dd3923bc2a96`), #3490 (`a182087c6972`), #3453
  (`e1b949d7e475`), #3464 (`ed1789de5cdd`), #3467 (`2735d9ff8c40`), #3487
  (`b9ccae9b38e0`). En attente : #3447 (`freeze-after-verdict`), #3456
  (`verdicts-missing`), #3457 (`branch-not-published`), #3460, #3468, #3469
  (`blocked-verdict`), #3471 (`verdicts-missing`).
  **Recalcul de contrôle à 05:12Z**, après l'avis de l'auditeur sur #3447 :
  **11 lots prêts**, 7 en attente, 0 erreur ; #3447 (`6f08ac6b0a01`) entre
  dans le véhicule, #3494 (créé 04:44Z) apparaît en attente
  (`verdicts-missing` sur `52f5746d`).
  Écart d'hôte signalé : `scripts/paquet-candidats.py --project ameesh` refuse
  sur cet hôte (`project 'ameesh' is not declared in radar.projects (declared:
  none)`) ; la liste a été produite par le **même module**
  (`packet_candidates.inventory`) avec le projet `ameesh` et la cible `main`
  explicites, sans écriture (même écart que le manifeste du 23/10 09:00).

## Admissions (11 lots, 11 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3450 | `52aa09fd948b` | sensitive | 2/2 : ameesh-dev4 (claude) 04:47Z ; ameesh-cx2 (codex) 04:56Z sur `52aa09fd` | Carte périmée : le lot garde impactés + contrat, mesure à 8+6 parts, re-pin du contrat #2446 |
| #3451 | `da667e031d12` | sensitive | 2/2 : ameesh-cx3 (codex) ; mty-amh-qa-01 (deepseek) sur `da667e03` | Courrier informatif : plus de tour de pilote sans geste attendu |
| #3454 | `7b8c47372968` | sensitive | 2/2 : ameesh-cx1 (codex), ameesh-cx3 (codex) ; auditeur (claude) sur `7b8c4737` | decide : une option suivie de la mention du bureau est reconnue |
| #3480 | `efe8e1278b92` | light | 2/1 : ameesh-cx3 (codex), ameesh-dev7 (claude) sur `efe8e1278` | Prise automatique : un agent éphémère reste hors prise |
| #3483 | `dd3923bc2a96` | sensitive | 2/2 : ameesh-dev1 (claude), mty-amh-qa-01 (deepseek) sur `dd3923bc` | Train : l'épinglage du canon suit la porte après une fusion du canon |
| #3467 | `2735d9ff8c40` | sensitive | 2/2 : auditeur (claude), ameesh-dev3 (deepseek) sur `2735d9ff` | Gel sur un lot `intake` : entrée en `qa`, refus intake→qa corrigé |
| #3490 | `a182087c6972` | sensitive | 2/2 : ameesh-dev4 (claude), mty-amh-qa-01 (deepseek) sur `a182087c` | Concurrence : ordre des verrous migration/lot unifié (empilé sur #3467) |
| #3453 | `e1b949d7e475` | sensitive | dispense pilote (#1979) : verdicts acquis sur `43802dbbf804` (ameesh-cx3 codex, ameesh-dev5 deepseek), range-diff lu par mesh-design 03:24Z | Panier #3426 : avertissement de relecture filtré par projet |
| #3464 | `ed1789de5cdd` | light | 1/1 : ameesh-dev1 (claude) sur `ed1789de` (OK reporté, mesh-design 04:53Z) | Panier #3452 : cost turns — troncature prouvée seulement |
| #3487 | `b9ccae9b38e0` | sensitive | 2/2 : ameesh-cx3 (codex), ameesh-dev4 (claude) sur `b9ccae9b` | Panier « slo » : alertes et dette de suivi |
| #3447 | `6f08ac6b0a01` | sensitive | 2/2 : ameesh-cx2 (codex), auditeur (claude) sur `6f08ac6b` (05:10Z) | Prise automatique : un lot réservé à un agent externe est pris par un agent qui doit le rendre |

#3467 est un ancêtre de #3490 (les deux têtes sont empilées) : les deux lots
sont composés en deux unités successives, #3467 d'abord, pour que chaque unité
nomme sa propre tête (forme `#<lot> <tête>` de #2360).

## Composition (11 unités, 11 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3450 | `52aa09fd948b` | `697e62efb848` | 0 conflit |
| 2 | #3451 | `da667e031d12` | `fd4115235933` | 0 conflit |
| 3 | #3454 | `7b8c47372968` | `25dbf5cc9387` | 0 conflit |
| 4 | #3480 | `efe8e1278b92` | `6a8a04509cc2` | 0 conflit |
| 5 | #3483 | `dd3923bc2a96` | `44bb490ce4f0` | 0 conflit |
| 6 | #3467 | `2735d9ff8c40` | `070af3496c98` | 0 conflit |
| 7 | #3490 | `a182087c6972` | `8478a2322bff` | 0 conflit (empilé sur #3467) |
| 8 | #3453 | `e1b949d7e475` | `a93f8099779e` | 0 conflit |
| 9 | #3464 | `ed1789de5cdd` | `715313518550` | 0 conflit |
| 10 | #3487 | `b9ccae9b38e0` | `09676d9d9e3a` | 0 conflit |
| 11 | #3447 | `6f08ac6b0a01` | `c428fa42c587` | 1 conflit texte résolu (`alert_briefs.py`) |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête (`vehicule 1600 : #<lot> <tête>`) et il porte un trailer
`ameesh-work:`. La base n'a pas bougé pendant la composition.

## Conflit résolu (unité 11)

`src/ameesh/alert_briefs.py` : #3487 (HEAD) réécrit le message de l'assigné du
`blocked_freeze` (« deliver <raison> », avec la raison « fix owed after
returning to build » ou « blocking verdict of X ») ; #3447 ajoute une branche
`elif external:` (lot réservé à un exécuteur externe) juste après ce bloc.
Résolution : le texte de #3487 **conservé** (le `reason` couvre les deux cas
que #3447 laissait au seul `blocker`) et la branche `external` de #3447
**ajoutée** telle quelle. Les deux intentions sont préservées, aucune
conception nouvelle : conflit mécanique, aucun avis supplémentaire (§2, §11 bis).

## Contrôles

- **Liste calculée** : 11 prêts, **11/11 composés**, 0 écarté, 0 erreur.
- **Conflits** : 10/11 unités fusionnées sans intervention, 1 conflit texte
  résolu (ci-dessus) ; aucun marqueur de conflit ; `git diff --check` vert.
- **Tests ciblés de l'arbre intégré** : **974 tests ciblés verts** (5 sautés), aucun échec, sur les modules impactés (4 shards : 288 + 303 + 263 + 120 ; les deux shards restants ont été arrêtés par la rotation de session, leurs modules passent dans la suite complète de la CI — résultat non concluant, non bloquant, §3) ; `test_3447_prise_externe` 10/10, `test_3288_fixes_owed` 23/23 sur la tête intégrée
- **Contrôles statiques du gel** (`scripts/freeze-checks.py`) : **6/6 verts**
  (garde mécanique #2449, compilation, attribution #1632, lint L63, alert
  types #1060, doc readers L161a).
- **Prévalidation** : à lire au journal du gel (la carte mesurée est périmée,
  179 h — c'est l'objet de #3450) ; non bloquante, motif exact consigné au lot.
- **Diff** : 62 fichiers, +2828/−309 hors manifeste ; classe sensible (CI
  `carte.yml`, `scripts/test-plan.py`, `train_*`, `freeze_gate`, `storage/`,
  `review_lot`, `work.py`, migrations additives `0482`/`0483`, docs
  d'exploitation).
- **Migrations (staging)** : deux migrations additives entrent,
  `src/ameesh/migrations/0482_work_item_external_executor.sql` (#3447) et
  `src/ameesh/migrations/0483_overview_passif_info.sql` (#3451). Elles
  s'appliquent en staging **avant** de relancer les exécuteurs (§11 bis, §14).
- **Inclusion** : `scripts/paquet-inclusion.py` rc 0 sur la tête (attendus =
  les 11 admis ; prêts = les 11 du recalcul ; 0 omission).
- **Suite complète** : demandée sur la tête finale, en mode paquet ; les
  verdicts unitaires couvrent les contenus, le paquet est relu sur sa tête par
  les familles du porteur (§12).
- **Fusion** : sous garde de tête, dès le vert et les avis du porteur ;
  landings `ameesh work landing <unité> --vehicle 3493` déclarés au gel.

## En attente (recalcul, hors paquet)

| lot | motif |
|---|---|
| #3456 | `verdicts-missing` : un avis (dev7) sur `3581d8f17455`, il en faut deux (sensible) |
| #3457 | `branch-not-published` : `auditeur/session-taille-pilotes` absente d'origin |
| #3460 | `blocked-verdict` : gel refusé |
| #3468 | `blocked-verdict` : gel refusé |
| #3469 | `blocked-verdict` : gel refusé |
| #3471 | `verdicts-missing` : un avis (dev2) sur `88a6f3711f49`, indépendance non prouvée |
| #3494 | `verdicts-missing` : aucun avis sur le gel `52f5746d` (sensible, deux familles requises) |

## Écartés (ce véhicule)

Aucun lot prêt n'est écarté : les 11 lots prêts du recalcul de 05:12Z sont
composés. #3430 (app nexlink, cible `develop`) est hors du `main` d'ameesh.

## Reprise CI du 25/10 05:55Z

La suite complète du run `40166201082` (tête `b24b8cd1`) a rendu rouge
`tests.test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue`
(psycopg 4/8) : `work_items.external_executor` (migration 0482, #3447) n'était
ni accordée ni exclue au contrat des rôles superviseur. Correctif sur le
véhicule, `deploy/sql/role-superviseur.sql` : la colonne est un nom d'agent ou
d'humain, de l'ÉTAT comme `assignee` et `reviewer`, et entre au contrat (aucun
droit retiré, aucune autre colonne). Témoin local :
`test_chaque_colonne_est_accordee_ou_exclue` vert (45 s, banc 55432). Classe du
delta propre : sensible (SQL) → deux avis de familles différentes sur la
nouvelle tête, puis suite complète.

## Têtes précédentes

`b24b8cd1` : première tête de composition (11 unités), CI rouge sur le seul
classement `work_items.external_executor` (run `40166201082`) ; deux verdicts
rendus sur son delta propre (auditeur et mesh-design, 05:41Z — même famille).
Base `cc93c5927` inchangée depuis h-15.

## CI

Suite complète unique portée par le run `pull_request` de la PR sur la tête
finale qui porte ce manifeste, étiquette `ci-full-<tête>` ; le portier
`paquet.yml` ne lance rien pour une tête couverte par la PR. Un rouge après
fusion se corrige en avant ; un lot fautif est retiré sans relire le reste
(§11 bis).
