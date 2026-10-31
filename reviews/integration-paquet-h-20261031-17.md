# Paquet integration/paquet-h-20261031-17 — composition (§12), sur main `f449ef772012`

Base : `main` **`f449ef77201211bdf193cb15346efef424b68644`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-17`, cible `main`.
Porteur : **#5137**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5129 | `6eec721a13b8` |
| #5007 | `ddf1739c230e` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5129 | `6eec721a13b8` | `903839892bef` | composition |
| 2 | #5007 | `ddf1739c230e` | `3ace92f4ddfb` | composition |

## Écartés

| lot | motif |
|---|---|
| #5041 | rouge ci-full 41222711899 (3 tests : test_l109_mediated_mode x2, test_merged_work_item_tracking), rejoué rouge sur #5041 seul — --exclude, recomposition sans lui |
| #4972 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5134 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 4 lot(s) prêt(s), 7 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 24.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
