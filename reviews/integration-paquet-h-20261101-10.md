# Paquet integration/paquet-h-20261101-10 — composition (§12), sur main `7bae762ec109`

Base : `main` **`7bae762ec109a6cc0fa68ece5d015ee9309fb8a8`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261101-10`, cible `main`.
Porteur : **#5273**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5244 | `041d0c1f4650` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5244 | `041d0c1f4650` | `0b0be206715f` | composition |

## Écartés

| lot | motif |
|---|---|
| #5287 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5250 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5293 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 4 lot(s) prêt(s), 4 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 33.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
