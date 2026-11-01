# Paquet integration/paquet-h-20261101-03 — composition (§12), sur main `fc7c96567fae`

Base : `main` **`fc7c96567fae052bb47f0d0e99de5c414b26ff21`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261101-03`, cible `main`.
Porteur : **#5220**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (8 unités, 8 lots)

| lot | source composée |
|---|---|
| #5189 | `abd766ea0034` |
| #5203 | `9e9ef12ecee8` |
| #5204 | `6c7256c32e0a` |
| #5206 | `fdcf5ac8cd0e` |
| #5215 | `e92989965b56` |
| #5160 | `1d3d39214217` |
| #5177 | `ad02e60203e4` |
| #5209 | `2e1e78c554f1` |

## Composition (8 unités, 8 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5189 | `abd766ea0034` | `763338c0f904` | composition |
| 2 | #5203 | `9e9ef12ecee8` | `8fb18ecea028` | composition |
| 3 | #5204 | `6c7256c32e0a` | `7b67c77386aa` | composition |
| 4 | #5206 | `fdcf5ac8cd0e` | `bd26851bb641` | composition |
| 5 | #5215 | `e92989965b56` | `626364a894e6` | composition |
| 6 | #5160 | `1d3d39214217` | `408dbbcd0c03` | composition |
| 7 | #5177 | `ad02e60203e4` | `4c06fe54d4a6` | composition |
| 8 | #5209 | `2e1e78c554f1` | `f1b47529d62b` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 8 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 25.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
