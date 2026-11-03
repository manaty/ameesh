# Paquet integration/paquet-h-20261103-04 — composition (§12), sur main `ab776f2dca59`

Base : `main` **`ab776f2dca59f157edb33163c1d415c58ab1f0bb`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-04`, cible `main`.
Porteur : **#5560**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5555 | `62f8040cf0e1` |
| #5552 | `d008d90c97aa` |
| #5553 | `951062150fa6` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5555 | `62f8040cf0e1` | `271d7feb7573` | composition |
| 2 | #5552 | `d008d90c97aa` | `ef5052c33636` | composition |
| 3 | #5553 | `951062150fa6` | `ace52ad88b75` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 41.0 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
