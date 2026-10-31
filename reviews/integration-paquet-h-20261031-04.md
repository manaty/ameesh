# Paquet integration/paquet-h-20261031-04 — composition (§12), sur main `44d641815f78`

Base : `main` **`44d641815f78aefaa22da2e2882b935ff5a08520`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-04`, cible `main`.
Porteur : **#5065**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (8 unités, 8 lots)

| lot | source composée |
|---|---|
| #4781 | `f72fc378161d` |
| #4818 | `4201b91fbf72` |
| #5026 | `5eaad19299b3` |
| #5042 | `c12bd9b3e502` |
| #5043 | `2d0c8271b0aa` |
| #5044 | `571d68d3f034` |
| #5052 | `1eb3178a27b2` |
| #4411 | `8d500cdbf7cc` |

## Composition (8 unités, 8 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4781 | `f72fc378161d` | `e3014250a237` | composition |
| 2 | #4818 | `4201b91fbf72` | `8efc87ce8abf` | composition |
| 3 | #5026 | `5eaad19299b3` | `59de3aed754f` | composition |
| 4 | #5042 | `c12bd9b3e502` | `260538dc1dd6` | composition |
| 5 | #5043 | `2d0c8271b0aa` | `5f774a4859f4` | composition |
| 6 | #5044 | `571d68d3f034` | `05dc099b1bc7` | composition |
| 7 | #5052 | `1eb3178a27b2` | `e1fb15452433` | composition |
| 8 | #4411 | `8d500cdbf7cc` | `eacd4bd82d98` | composition |

## Écartés

| lot | motif |
|---|---|
| #5045 | test_4473 rouge sur 785990c1661d (run 41167039826) ; correctif de test 2791ea04bbfb en relecture, paquet suivant — --exclude, recomposition sans lui |
| #5015 | test_3244 rouge sur c8d3ad95d989 (run 41167039826) ; correctif de test b3d2f2fded1a en relecture, paquet suivant — --exclude, recomposition sans lui |
| #4975 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5037 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 10 lot(s) prêt(s), 4 en attente, 4 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 23.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
