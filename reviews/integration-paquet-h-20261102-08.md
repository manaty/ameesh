# Paquet integration/paquet-h-20261102-08 — composition (§12), sur main `f0e5b2e6daca`

Base : `main` **`f0e5b2e6daca6ba6487e2353cdfe933714e04cb4`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-08`, cible `main`.
Porteur : **#5467**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (7 unités, 7 lots)

| lot | source composée |
|---|---|
| #5404 | `6ddbdb339f0d` |
| #5422 | `acfeef7749a8` |
| #5459 | `7bd18a620948` |
| #5351 | `b7b279d57033` |
| #5428 | `3f3bf31ba816` |
| #5460 | `aaf902836d4d` |
| #5461 | `092d390dcb37` |

## Composition (7 unités, 7 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5404 | `6ddbdb339f0d` | `f78e1840186d` | composition |
| 2 | #5422 | `acfeef7749a8` | `5b39cafdfa32` | composition |
| 3 | #5459 | `7bd18a620948` | `c9ee87eece89` | composition |
| 4 | #5351 | `b7b279d57033` | `1a8058b6906c` | composition |
| 5 | #5428 | `3f3bf31ba816` | `6246d1f2c757` | composition |
| 6 | #5460 | `aaf902836d4d` | `dcda54862639` | composition |
| 7 | #5461 | `092d390dcb37` | `dfd31e932f3f` | composition |

## Écartés

| lot | motif |
|---|---|
| #5427 | exclu par le pilote — --exclude, recomposition sans lui |
| #5411 | exclu par le pilote — --exclude, recomposition sans lui |

Liste calculée : 7 lot(s) prêt(s), 4 en attente, 2 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 30.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
