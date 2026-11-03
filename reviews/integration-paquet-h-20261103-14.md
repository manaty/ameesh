# Paquet integration/paquet-h-20261103-14 — composition (§12), sur main `6e2e6795b126`

Base : `main` **`6e2e6795b12672dfafb949b51692cbc7df979807`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-14`, cible `main`.
Porteur : **#5645**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #5630 | `1e0caba77394` |
| #5637 | `2e4c63f0f2a3` |
| #5602 | `10d187b2ae73` |
| #5632 | `d236a2c3a088` |
| #5589 | `8beb0408ebb2` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5630 | `1e0caba77394` | `fab54e901e5f` | composition |
| 2 | #5637 | `2e4c63f0f2a3` | `d2450c87973d` | composition |
| 3 | #5602 | `10d187b2ae73` | `1b2da3f4b40b` | composition |
| 4 | #5632 | `d236a2c3a088` | `9d3d395b8991` | composition |
| 5 | #5589 | `8beb0408ebb2` | `6fe91da37078` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 5 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 43.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
