# Paquet integration/paquet-h-20261103-20 — composition (§12), sur main `1f2c7c7b695b`

Base : `main` **`1f2c7c7b695b7a46113937f7f6a2b8dbebd85e07`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-20`, cible `main`.
Porteur : **#5679**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5591 | `e994765fe058` |
| #5653 | `a151c1b33104` |
| #5654 | `6a4949b57bd8` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5591 | `e994765fe058` | `f31b955c47c8` | composition |
| 2 | #5653 | `a151c1b33104` | `cdab0301a7c3` | composition |
| 3 | #5654 | `6a4949b57bd8` | `a3b7002c73af` | composition |

## Écartés

| lot | motif |
|---|---|
| #5680 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 4 lot(s) prêt(s), 2 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 46.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
