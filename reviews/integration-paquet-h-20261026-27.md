# Paquet integration/paquet-h-20261026-27 — composition (§12), sur main issu de h-26

Base : `main` **`6d67072c378762dac4803e5255cab0be9e4f2e89`** (fusion du paquet
h-26, PR #2837, 26/10 07:36Z). Branche
`integration/paquet-h-20261026-27`, cible `main`. Porteur : **#3681**.
Composeur : mty-amh-impl-01 (deepseek, agents-1).

Déclencheur : fusion de h-26 (`main` 6d67072c, 07:36Z) et consigne mesh-design
du 26/10 07:43Z (lot #3681 : 8 unités et motifs des écartés). Liste calculée
`paquet-candidats` sur `6d67072c` relancée à 07:59Z : **11 prêts**, 5 en
attente, 3 erreurs de provenance (chaîne handoff, hors paquet par consigne
pilote). Les 11 prêts sont composés : les 8 du corps du lot, #3634 (écarté en
h-26, avis claude + codex acquis sur `ecf26b1d`), #3661 (prêt au calcul, ajouté
sur consigne), puis **#3642** (rebasé sur `6d67072c` à 07:47Z, avis dev4 à
07:50Z) et **#3473** (gel `3782ae6b`, avis auditeur, provenance redevenue
lisible à 07:59Z), apparus prêts pendant la composition et composés plutôt
qu'écartés (canon §12). #3576 reste écarté (gel ≠ tête publiée, conflit
`docs/EXECUTEUR-MEDIEE.md`).

## Admissions (11 lots, 11 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3584 | `9bea1efbfff0` | 2/2 (auditeur claude, cx2 codex) | Porte d'avis : un auteur prouvé sous plusieurs familles exclut chacune de ses familles |
| #3661 | `a6b387de827a` | 2/2 (cx2 codex, dev3 deepseek) | Panier #3596 : plancher d'attente partielle — un siège retiré n'est plus lu |
| #3648 | `df38f305190d` | 2/2 (cx2 codex, dev3 deepseek) | `merge-pr` : le scratch de la porte porte sa forge |
| #3647 | `95941571753a` | 2/2 (cx2 codex, dev3 deepseek) | Rythme des comptes : `pace_multiplier` compilé du canon |
| #3634 | `ecf26b1d629e` | 2/2 (mesh-design claude, cx1 codex) | SLO merge : la composition doit être celle de la tête gelée du porteur |
| #3644 | `48e329a74cc5` | 1 requis, 2 rendus (cx1, cx2 codex) | Banc #3632 : le niveau SLO ne dépend plus du p80 de CI de l'hôte |
| #3640 | `f3527ac8a4c3` | 1/1 (cx2 codex, reporté de `28f9badf`) | Coupe Nexlink : une tête à l'arbre d'une PR de paquet verte vaut verte |
| #3672 | `4fff8cbe1cab` | 1/1 (cx2 codex) | Débit : un lot fermé n'attend plus |
| #3626 | `7275eb2b544c` | 1/1 (dev2 claude) | Carte mesurée : documenter la perte de lien des sous-processus |
| #3642 | `c69b738522b2` | 1/1 (dev4 claude) | Témoins de #3639 : total qui baisse, lecture par `commit_ci`, quota 403 |
| #3473 | `3782ae6bcae8` | 1/1 (auditeur claude ; 2e avis impl-05 deepseek acquis) | Handoff réel H6 : CLI opérateur advance, cancel, retry |

## Avis au gel

| lot | gel | verdicts |
|---|---|---|
| #3584 | 19709 | #19724 auditeur (claude) ok, #19715 cx2 (codex) ok |
| #3661 | 19815 | #19820 dev3 (deepseek) ok, #19819 cx2 (codex) ok |
| #3648 | 19678 | #19687 dev3 (deepseek) ok, #19681 cx2 (codex) ok |
| #3647 | 19828 | #19857 dev3 (deepseek) ok, #19838 cx2 (codex) ok |
| #3634 | 19621 | #19622 mesh-design (claude) ok, #19716 cx1 (codex) ok |
| #3644 | 19720 | #19731 cx2 (codex) ok, #19728 cx1 (codex) ok |
| #3640 | 19882 | #19885 cx2 (codex) ok, reporté par alignement de `28f9badf` |
| #3672 | 19802 | #19806 cx2 (codex) ok |
| #3626 | 19756 | #19839 dev2 (claude) ok |
| #3642 | 19887 | #19891 dev4 (claude) ok |
| #3473 | 19226 | #19227 auditeur (claude) ok |

## Composition (11 unités, 11 lots)

| unité | lot | source | commit |
|---|---|---|---|
| 1 | #3584 | `9bea1efbfff0` | `e38d12337a0f` |
| 2 | #3661 | `a6b387de827a` | `bef1822ec9cd` |
| 3 | #3648 | `df38f305190d` | `70682556ee9a` |
| 4 | #3647 | `95941571753a` | `9c02704c219d` |
| 5 | #3634 | `ecf26b1d629e` | `5b0f18d6d108` |
| 6 | #3644 | `48e329a74cc5` | `f44d3cf65665` |
| 7 | #3640 | `f3527ac8a4c3` | `b09d120b1697` |
| 8 | #3672 | `4fff8cbe1cab` | `9d9a26efb39f` |
| 9 | #3626 | `7275eb2b544c` | `19d7775ceda8` |
| 10 | #3642 | `c69b738522b2` | `1bb7cfc98000` |
| 11 | #3473 | `3782ae6bcae8` | `6b703fc45e3d` |

Les 11 unités sont des fusions propres : aucun conflit mécanique, aucune
résolution d'union, aucune adaptation de test. #3584 est composé en premier
parce que #3473 le déclare en dépendance (elle-même composée après lui) et que
#3661 touche les mêmes modules de désignation. #3642 et #3473 sont composés
en dernier, après leur entrée dans la liste des prêts.

## Écartés de h-27

| lot | motif |
|---|---|
| #3519 | gel refusé (`freeze-after-verdict`) |
| #3526 | 1 avis sur 2 requis (sensible), verdicts manquants et conflit `handoff_factory.py` / `runner.py` / `test_3472_handoff_runner.py` |
| #3576 | gel `30750a74` ≠ tête publiée `98ef370b` (`freeze-not-head`) et conflit `docs/EXECUTEUR-MEDIEE.md` |
| #3641 | gel `8be9d7fd` sans ses 2 avis indépendants : un OK cx1 (codex) non prouvé indépendant (pas de lecture d'auteur) |
| #3658 | gel `4aa0f2a0` sans avis (2 requis, sensible) ; empilé sur #3584 |
| #3474 | erreur de lecture de la porte : provenance d'auteur inconnue sur les commits gelés (chaîne handoff) |
| #3506 | erreur de lecture de la porte : provenance d'auteur inconnue sur les commits gelés (chaîne handoff) |
| #3509 | erreur de lecture de la porte : provenance d'auteur inconnue sur les commits gelés (chaîne handoff) |

Les erreurs de provenance de la chaîne handoff (#3474, #3506, #3509) restent
hors paquet par consigne pilote (même motif en h-22/h-23/h-24/h-25/h-26) :
elles ne rendent pas la liste des lots prêts incomplète pour ce paquet. #3473,
qui portait la même erreur au calcul de 07:47Z, est redevenu lisible à
07:59Z (porte ok, avis auditeur acquis) et a été composé. Les lots
`already-in-target` (#3389, #3604, #3625, #3629 à #3631, #3633, #3639)
attendent leur clôture par `sync-merges` ; ce ne sont pas des unités.

## Contrôles

- Liste calculée `paquet-candidats` sur `6d67072c` relancée à 07:59Z : 11 lots
  prêts, 5 en attente, 3 erreurs de provenance ; 11 prêts composés, 5 écartés
  avec motif, 3 erreurs hors paquet par consigne pilote.
- Composition : 11 unités sur `6d67072c3787` ; aucune fusion en conflit,
  aucune résolution d'union, aucune adaptation.
- Contrôles statiques de gel (`scripts/freeze-checks.py`, au gel du porteur).
- Contrôle d'inclusion `scripts/paquet-inclusion.py` sur la tête du manifeste.
- Une seule suite complète sur la tête gelée, après le gel (`ameesh ci run
  3681 --full`), pas de CI par lot (§11 bis).
- Aucune migration additive dans ce paquet : rien à appliquer en staging.
