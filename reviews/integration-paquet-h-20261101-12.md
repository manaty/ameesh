# Paquet integration/paquet-h-20261101-12 — composition (§12), sur main `cdf2ba37c115`

Base : `main` **`cdf2ba37c115e63d3c9eb1ba3d10484f9f748d8f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261101-12`, cible `main`.
Porteur : **#5319**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #5298 | `14f65fdc574b` |
| #5301 | `3839199fbb99` |
| #5302 | `53271d52bfd3` |
| #5312 | `132d8b7ecf3b` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5298 | `14f65fdc574b` | `e9c928e41f0b` | composition |
| 2 | #5301 | `3839199fbb99` | `c3bf53c84dc6` | composition |
| 3 | #5302 | `53271d52bfd3` | `842f09f6516f` | composition |
| 4 | #5312 | `132d8b7ecf3b` | `86a5bffa21d0` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 35.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
