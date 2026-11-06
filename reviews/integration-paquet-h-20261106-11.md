# Paquet integration/paquet-h-20261106-11 — composition (§12), sur main `a13e6c6114a2`

Base : `main` **`a13e6c6114a26a2c12dcaea7f7bc570c76635f27`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261106-11`, cible `main`.
Porteur : **#6032**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6017 | `9676446b321c` |
| #6012 | `6fee6d0ed68c` |
| #6014 | `ef7ec9d72584` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6017 | `9676446b321c` | `06c5e33034d3` | composition |
| 2 | #6012 | `6fee6d0ed68c` | `9d28a4d928bf` | composition |
| 3 | #6014 | `ef7ec9d72584` | `d0ed9b961e95` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 70.0 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
