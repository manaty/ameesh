# Paquet integration/paquet-h-20261109-17 — composition (§12), sur main `65c82625b113`

Base : `main` **`65c82625b113a03641bae38e4b687e827d42295a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261109-17`, cible `main`.
Porteur : **#6562**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #6544 | `9cf8817e6d73` |
| #6561 | `c1533d998c71` |
| #6372 | `5c161122e6fa` |
| #6487 | `92290a2f5bfb` |
| #6537 | `7c9de2da9501` |
| #6556 | `d2ef5e85c081` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6544 | `9cf8817e6d73` | `17b7353aad3a` | composition |
| 2 | #6561 | `c1533d998c71` | `6aa117d7e1c5` | composition |
| 3 | #6372 | `5c161122e6fa` | `ff87c2e3c744` | composition |
| 4 | #6487 | `92290a2f5bfb` | `35d650fe8b32` | composition |
| 5 | #6537 | `7c9de2da9501` | `eefde232b76f` | composition |
| 6 | #6556 | `d2ef5e85c081` | `69d312fea643` | composition |

## Écartés

| lot | motif |
|---|---|
| #6534 | coupure suivante : tête regelée — composé 25abf298a4e2, tête actuelle 6f28dbcaa11b : pas de ci de paquet sur la nouvelle tête |

Liste calculée : 7 lot(s) prêt(s), 3 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 57.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
