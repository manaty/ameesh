# Paquet integration/paquet-h-20261105-06 — composition (§12), sur main `65914cd09ec1`

Base : `main` **`65914cd09ec119d6f56dc87c8ed0712bd1f408bc`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-06`, cible `main`.
Porteur : **#5875**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5860 | `9c6a0f2cb4f5` |
| #5861 | `968cf4428e25` |
| #5863 | `d0f31e7f3c78` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5860 | `9c6a0f2cb4f5` | `94cd09541f82` | composition |
| 2 | #5861 | `968cf4428e25` | `58a84920055a` | composition |
| 3 | #5863 | `d0f31e7f3c78` | `0cb5120a686d` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 57.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
