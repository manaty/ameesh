# Paquet integration/paquet-h-20261103-05 — composition (§12), sur main `3d69d47c4ffa`

Base : `main` **`3d69d47c4ffa5099dd916c4a625465b8cdf68447`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-05`, cible `main`.
Porteur : **#5564**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5559 | `2ef34f866b34` |
| #5557 | `4be03a5c891b` |
| #5558 | `50952c7182e8` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5559 | `2ef34f866b34` | `2285047132bd` | composition |
| 2 | #5557 | `4be03a5c891b` | `9ed59b33f20c` | composition |
| 3 | #5558 | `50952c7182e8` | `8dff682f1e8a` | composition |

## Écartés

| lot | motif |
|---|---|
| #5556 | tête regelée 35df97bae181 après composition (source composée 48c191ed439d : tests.test_5191_canon_check_avis en erreur) ; au paquet suivant — --exclude, recomposition sans lui |
| #5563 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 4 lot(s) prêt(s), 3 en attente, 2 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 43.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
