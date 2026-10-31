# Paquet integration/paquet-h-20261031-10 — composition (§12), sur main `cb75328bd7e8`

Base : `main` **`cb75328bd7e80205efeffe25540d2d76fd3301a4`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-10`, cible `main`.
Porteur : **#5092**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5082 | `f7aa23c973ad` |
| #5055 | `b020f91c86df` |
| #5087 | `1f63a66a8e6d` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5082 | `f7aa23c973ad` | `d6f945f354c0` | composition |
| 2 | #5055 | `b020f91c86df` | `5f064907a2c1` | composition |
| 3 | #5087 | `1f63a66a8e6d` | `b6516a5d32ff` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 23.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
