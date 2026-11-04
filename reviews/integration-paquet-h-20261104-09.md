# Paquet integration/paquet-h-20261104-09 — composition (§12), sur main `1203bb27b72a`

Base : `main` **`1203bb27b72aeb8cc31dbd4bbcf0c6ba14c1fd50`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-09`, cible `main`.
Porteur : **#5736**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5729 | `65c50d91b55f` |
| #4306 | `e120e4df1d19` |
| #5706 | `ac5ad0cf5a8a` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5729 | `65c50d91b55f` | `3a206c218ad7` | composition |
| 2 | #4306 | `e120e4df1d19` | `b57f39757d33` | composition |
| 3 | #5706 | `ac5ad0cf5a8a` | `0d69c02d490c` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 40.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
