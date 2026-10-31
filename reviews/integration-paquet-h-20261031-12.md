# Paquet integration/paquet-h-20261031-12 — composition (§12), sur main `f96bdd90a59a`

Base : `main` **`f96bdd90a59a48c62369c9415551f2fede912b1f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-12`, cible `main`.
Porteur : **#5102**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5038 | `b422c7473994` |
| #5094 | `e7fe08ed6b8f` |
| #5097 | `e48d3bfb7fbc` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5038 | `b422c7473994` | `00105f1ab324` | composition |
| 2 | #5094 | `e7fe08ed6b8f` | `af40f08f072c` | composition |
| 3 | #5097 | `e48d3bfb7fbc` | `cf4cd85bd936` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 5 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 23.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
