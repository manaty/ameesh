# Paquet integration/paquet-h-20261031-12 — composition (§12), sur main `f96bdd90a59a`

Base : `main` **`f96bdd90a59a48c62369c9415551f2fede912b1f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-12`, cible `main`.
Porteur : **#5102**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5094 | `e7fe08ed6b8f` |
| #5097 | `e48d3bfb7fbc` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5094 | `e7fe08ed6b8f` | `060f4fb476d8` | composition |
| 2 | #5097 | `e48d3bfb7fbc` | `50b51e01e590` | composition |

## Écartés

| lot | motif |
|---|---|
| #5038 | 52 rouges CI (parts psycopg 3/8 et 5/8, run 41201766881) attribués à #5038 — --exclude, recomposition sans lui |
| #5057 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5099 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 4 lot(s) prêt(s), 4 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 23.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
