# Paquet integration/paquet-h-20261108-04 — composition (§12), sur main `9f3367e73e0b`

Base : `main` **`9f3367e73e0b55951b70749fb4c89d8b1e45e6f0`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261108-04`, cible `main`.
Porteur : **#6260**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #6185 | `2d1dd5611f18` |
| #6252 | `485b01c9a183` |
| #6255 | `e7b9da902333` |
| #6256 | `509765902ad8` |
| #6243 | `2d9f6451254d` |
| #6249 | `3ce532250db4` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6185 | `2d1dd5611f18` | `a5d2860857db` | composition |
| 2 | #6252 | `485b01c9a183` | `82228d294df5` | composition |
| 3 | #6255 | `e7b9da902333` | `59e30482fb2f` | composition |
| 4 | #6256 | `509765902ad8` | `a02180721947` | composition |
| 5 | #6243 | `2d9f6451254d` | `363b419f8819` | composition |
| 6 | #6249 | `3ce532250db4` | `0f5ec4a3b765` | composition |

## Écartés

| lot | motif |
|---|---|
| #6257 | rouge en CI complète : work.sync_validations (interface WorkItems) non classée dans la table du contrat exécuteur L107 (test_l_interface_entiere_est_classee) ; retiré sans relire le reste, canon §11 bis — --exclude, recomposition sans lui |
| #6238 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 7 lot(s) prêt(s), 4 en attente, 2 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 50.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
