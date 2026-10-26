# Paquet integration/paquet-h-20261026-26 — composition (§12), sur main issu de h-25

Base : `main` **`5d21c75347115d4777f31696e4da3d966fb7a669`** (fusion du paquet
h-25, PR #2819, 26/10 04:55:45Z). Branche
`integration/paquet-h-20261026-26`, cible `main`. Porteur : **#3637**.
Composeur : mty-amh-impl-01 (deepseek, agents-1).

Déclencheur : fusion de h-25 (auto-merge, `main` 5d21c753) et consignes
mesh-design des 26/10 03:40Z (triage h-26), 04:03Z (#3625), 04:09Z (#3634
empilé sur #3632), 04:20Z (#3643), 04:29Z (#3629) et 04:57Z (#3604 et #3631
inclus ; #3598, #3589 et #3629 dès leurs avis cx1 ; #3639 hors paquet).
Liste calculée `paquet-candidats` sur `5d21c753` (05:0xZ) : **11 prêts** ;
10 composés, #3634 hors avis (pas prêt au calcul) ; #3633, prêt et non cité
par le pilote, est composé (canon §12 : un lot prêt est composé ou écarté
avec un motif au manifeste). #3389, gelé à 05:04Z sur `9804af64` et relu par
mesh-design à 05:08Z (doc seule), est ajouté sur consigne pilote du 05:09Z
après vérification de son gel et de sa fusion à blanc ; #3639, prêt au calcul
mais d'abord écarté (consigne 04:57Z), est finalement composé sur consigne
pilote du 05:34Z (P1 à faire entrer avant la coupe de 10:00Z, CI de lot non
concluante). Au total : **12 unités, 12 lots**.

## Admissions (12 lots, 12 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3633 | `0d1504839558` | 1/1 (docs-01 deepseek) | Coupe release : une tête de `main` dont l'arbre égale celui d'une PR de paquet verte vaut verte (#2820) |
| #3589 | `1f32cf6fad5a` | 2/2 (auditeur claude, cx1 codex) | Gel d'un lot empilé : détecter les copies (même patch-id) des commits de la tête du lot parent (#2773) |
| #3598 | `d583fc8e7220` | 2/2 (auditeur claude, mesh-design claude, cx1 codex) | Feu : give/withdraw attendent le bail `fire:<lot>` au lieu de refuser au premier conflit (#2783) |
| #3631 | `168b9a622deb` | 2/2 (mesh-design claude, cx2 codex) | Base commune : timeouts de requêtes, redésignations perdues et coupures LISTEN ; migration additive 0488 (#2817) |
| #3625 | `0df02c76b59f` | 1 requis, 2 rendus (auditeur claude, mesh-design claude) | Panier « #3612 » : tampon des flux sensibles (#2812) |
| #3604 | `497a3aed14b6` | 2/2 (auditeur claude, cx2 codex) | Carte mesurée : shards psycopg et psql sans coupure à 60 min, parts et épingle du contrat de train ajustées (#2791) |
| #3630 | `0156d6d14b4f` | 1/1 (auditeur claude) | dead_runner : anti-rebond de 8 min avant le push au propriétaire, coupures de bail mesurées à 5,2 min |
| #3632 | `41c7c2a9bb9c` | 1/1 (auditeur claude) | SLO review GO → merge : `merge_observed` reconnaît les membres d'un véhicule déjà fusionné (#2818) |
| #3643 | `2a551f478262` | 2/2 (mesh-design claude, auditeur claude) | `host_lifecycle.critical_names` : rôles critiques lus par la politique, plus de liste en dur (#2829) |
| #3629 | `38133669b226` | 2/2 (impl-04 deepseek, cx1 codex) | Manifeste de paquet sans commit : lecture des unités par les trailers `ameesh-work` (#2816) |
| #3389 | `9804af64b3a4` | 1/1 (mesh-design claude) | Rapport du pilote résident 24 h (R1) — doc seule (#2833) |
| #3639 | `d9bf46e5515e` | 2/2 (impl-03 deepseek, cx2 codex) | `radar.commit_ci` : lecture paginée de tous les check-runs du commit (P1, coupe 10:00Z) (#2825) |

## Avis au gel

| lot | gel | verdicts |
|---|---|---|
| #3633 | 19610 | #19617 docs-01 (deepseek) ok |
| #3589 | 19344 | #19357 auditeur (claude) ok, #19645 cx1 (codex) ok |
| #3598 | 19384 | #19402 auditeur (claude) ok, #19605 mesh-design (claude) ok, #19644 cx1 (codex) ok |
| #3631 | 19596 | #19597 mesh-design (claude) ok, #19636 cx2 (codex) ok |
| #3625 | 19613 | #19614 auditeur (claude) ok, #19619 mesh-design (claude) ok |
| #3604 | 19553 | #19554 auditeur (claude) ok, #19635 cx2 (codex) ok |
| #3630 | 19581 | #19582 auditeur (claude) ok |
| #3632 | 19574 | #19578 auditeur (claude) ok |
| #3643 | 19624 | #19625 mesh-design (claude) ok, #19626 auditeur (claude) ok |
| #3629 | 19627 | #19632 impl-04 (deepseek) ok, #19646 cx1 (codex) ok |
| #3389 | 19666 | #19673 mesh-design (claude) ok |
| #3639 | 19608 | #19615 impl-03 (deepseek) ok, #19634 cx2 (codex) ok |

## Composition (12 unités, 12 lots)

| unité | lot | source | commit |
|---|---|---|---|
| 1 | #3633 | `0d1504839558` | `58543337d` |
| 2 | #3589 | `1f32cf6fad5a` | `ae35af2c4` |
| 3 | #3598 | `d583fc8e7220` | `93e35c961` |
| 4 | #3631 | `168b9a622deb` | `13639e1b6` |
| 5 | #3625 | `0df02c76b59f` | `73dc68354` |
| 6 | #3604 | `497a3aed14b6` | `623c3e134` |
| 7 | #3630 | `0156d6d14b4f` | `f1b8bfba2` |
| 8 | #3632 | `41c7c2a9bb9c` | `bcf500d5d` |
| 9 | #3643 | `2a551f478262` | `9aafdb44e` |
| 10 | #3629 | `38133669b226` | `19cc1f879` |
| 11 | #3389 | `9804af64b3a4` | `630858e77` |
| 12 | #3639 | `d9bf46e5515e` | `5cab476dd` |

Les 12 unités sont des fusions propres : aucun conflit mécanique, aucune
résolution d'union. Une adaptation de delta véhicule (compteurs du contrat
médié) est décrite ci-dessous. #3629 est composé en dixième : il modifie
`scripts/paquet-compose.py`, et les neuf unités précédentes ont été composées
avec la version de `main`, pas avec celle qu'il apporte ; #3389 (doc seule,
ajouté à 05:1xZ) est composé après lui avec cette nouvelle version.

## Adaptations mécaniques (§2, aucun avis)

1. **#3598 × #3631, contrat médié et compteurs de doc** : les deux lots
   ajoutent une opération d'interface refusée à distance (`land_holder` pour
   #3598 dans `fire`, `reachability` pour #3631 dans la base commune). La
   fusion de `src/ameesh/mediated_executor/contract.json` est propre et porte
   les deux entrées ; les compteurs cités par `docs/EXECUTEUR-MEDIEE.md`
   restaient à **437 refusées / 509 opérations** (chaque branche portait le
   même delta 436→437 et 508→509 depuis sa base). Recomputés sur l'arbre
   intégré : 72 opérations, **439 refusées**, interface entière **511**. Le
   document est ajusté dans `023a8da3c` ; `tests.test_l107_executor_contract`
   23/23 vert. Aucun avis demandé (§2).

## Écartés de h-26

| lot | motif |
|---|---|
| #3634 | pas prêt au calcul : gel `ecf26b1d629e` (sensible) sans ses 2 avis indépendants — un seul avis, mesh-design ; la consigne pilote du 04:09Z ne suffit pas à la porte |
| #3617 | rebase en cours par impl-03 depuis 05:00Z ; au moment de la composition son dernier gel reste `e4ab8cb7`, en conflit avec `main` sur `src/ameesh/review_designation_activation.py` — pas de nouveau gel avec avis : h-27 |
| #3584 | hold pilote (attente de h-25 levée) : rebase sur `main` puis 2 avis claude+codex sur le passage refus → exclusion ; conflit `src/ameesh/review_designation.py` |
| #3576 | hold pilote (siège codex tenu pour cx1) et conflit `docs/EXECUTEUR-MEDIEE.md` |
| #3526 | 1 avis sur 2 requis (sensible) et conflit handoff (`handoff_factory.py`, `runner.py`, `test_3472_handoff_runner.py`) |
| #3519 | lecture de la porte incomplète (`freeze-after-verdict`, provenance d'auteur) |
| #3473 | lecture de la porte incomplète (famille d'auteur ambiguë pour cx2) — chaîne handoff |
| #3474 | lecture de la porte incomplète (provenance d'auteur inconnue) — chaîne handoff |
| #3506 | lecture de la porte incomplète (provenance d'auteur inconnue) — chaîne handoff |
| #3509 | lecture de la porte incomplète (provenance d'auteur inconnue) — chaîne handoff |
| #3620 | sans branche : rien à composer |
| #3627 | véhicule de h-25, jamais une unité |

Les erreurs de lecture de la chaîne handoff (#3473, #3474, #3506, #3509,
#3519) restent hors paquet par consigne pilote (même motif en h-22/h-23/h-24/
h-25) : elles ne rendent pas la liste des lots prêts incomplète pour ce
paquet. Les lots `already-in-target` (#3602, #3608, #3611, #3614, #3619)
attendent leur clôture par `sync-merges` ; ce ne sont pas des unités.

## Contrôles

- Composition : 12 unités sur `5d21c7534711` ; aucune fusion en conflit,
  aucune résolution d'union ; une adaptation (compteurs du contrat médié,
  ci-dessus).
- Liste calculée `paquet-candidats` sur `5d21c753` : 12 lots prêts au total
  (11 au calcul de 05:0xZ, #3389 ajouté après son avis de 05:08Z) ; 12
  composés ; #3634 non prêt au calcul (avis manquants) et écarté au
  manifeste ; #3633, non cité par le pilote, composé ; #3639 ajouté sur
  consigne pilote du 05:34Z.
- Contrôles statiques de gel (`scripts/freeze-checks.py`) : 7/7 verts.
- Tests ciblés locaux sur l'arbre intégré : 166 tests OK (2448/3244 coupe
  release, 3589 gel empilé, 2157/2426 composition et clôture de paquet, 3606
  épingles du contrat de train, parts de la carte, 3643 noms critiques) ;
  23 tests du contrat exécuteur (l107) OK après l'adaptation des compteurs.
  Les modules de feu, désignation, notify et SLO sont couverts par la
  ci-full unique sur la tête gelée.
- Contrôle d'inclusion `scripts/paquet-inclusion.py` sur la tête du
  manifeste : aucun défaut ; 12 compositions, 12 contenus présents ;
  #3634 seul écarté au manifeste avec motif.
- Une seule suite complète sur la tête gelée, après le gel (`ameesh ci run
  3637 --full`), pas de CI par lot (§11 bis).
- La migration additive **0488** (#3631) est à appliquer en staging avant de
  relancer les exécuteurs (§14, migration additive seulement).
