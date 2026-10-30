# Paquet integration/paquet-h-20261030-05 — composition (§12), sur main `e8c64e31fcbc`

Base : `main` **`e8c64e31fcbc50f9c2f62ec7946c4c6ada78bc26`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-05`, cible `main`.
Porteur : **#4784**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #4765 | `19b8eaa8c6df` |
| #4772 | `c969c2ccf07d` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4765 | `19b8eaa8c6df` | `90d765bba4e4` | composition |
| 2 | #4772 | `c969c2ccf07d` | `3879e786f406` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 9 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 21.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
