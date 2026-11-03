# Paquet integration/paquet-h-20261103-13 — composition (§12), sur main `07ff13100c88`

Base : `main` **`07ff13100c88892a60f6a77bd2736969fb25d16c`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-13`, cible `main`.
Porteur : **#5636**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #5606 | `2adf8c3f7927` |
| #5583 | `5fa7062df84f` |
| #5604 | `72d2e7112deb` |
| #5627 | `366b48badeb5` |
| #5628 | `8b7d2519ba57` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5606 | `2adf8c3f7927` | `1951840d384b` | composition |
| 2 | #5583 | `5fa7062df84f` | `69cd14a27a8e` | composition |
| 3 | #5604 | `72d2e7112deb` | `22cf71b95eae` | composition |
| 4 | #5627 | `366b48badeb5` | `75a73cfdaae8` | composition |
| 5 | #5628 | `8b7d2519ba57` | `02807a27fa44` | composition |

## Écartés

| lot | motif |
|---|---|
| #5585 | erreur paquet-candidats, jamais composé — cannot read the review gate: author provenance unknown for frozen commit(s): b258df9d7292 |
| #5630 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5632 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5589 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 8 lot(s) prêt(s), 3 en attente, 4 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 45.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
