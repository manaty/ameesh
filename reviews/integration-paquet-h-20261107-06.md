# Paquet integration/paquet-h-20261107-06 — composition (§12), sur main `214328a78b4c`

Base : `main` **`214328a78b4c19788f24eee220529027189c33cc`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-06`, cible `main`.
Porteur : **#6109**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6093 | `0f4930757401` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6093 | `0f4930757401` | `939cf5ce6869` | composition |

## Écartés

| lot | motif |
|---|---|
| #6092 | coupure suivante : tête regelée — composé 221e058d4217, tête actuelle 1d1982de92a6 : pas de ci de paquet sur la nouvelle tête |
| #6091 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6104 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6079 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6096 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 6 lot(s) prêt(s), 3 en attente, 5 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 54.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
