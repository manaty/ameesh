# Paquet integration/paquet-h-20261030-17 — composition (§12), sur main `c6cefe0c6215`

Base : `main` **`c6cefe0c6215c2e39bf0e24e07c1f3a2514fdef5`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-17`, cible `main`.
Porteur : **#4985**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (11 unités, 11 lots)

| lot | source composée |
|---|---|
| #4791 | `e6fc30c41ab0` |
| #4756 | `d6c2d8afe08e` |
| #4867 | `16d97d27ec7e` |
| #4868 | `e2898c5c8677` |
| #4874 | `68c0d14e6d30` |
| #4901 | `c1a5e604f5ab` |
| #4969 | `57502f87f3c1` |
| #4748 | `75da0883382d` |
| #4849 | `5e1103cc5014` |
| #4850 | `dabdc06d210a` |
| #4888 | `564417662770` |

## Composition (11 unités, 11 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4791 | `e6fc30c41ab0` | `7e3286b896f0` | composition |
| 2 | #4756 | `d6c2d8afe08e` | `f1159e923b31` | composition |
| 3 | #4867 | `16d97d27ec7e` | `559fdc3a9ebe` | composition |
| 4 | #4868 | `e2898c5c8677` | `6d58b4cf838a` | composition |
| 5 | #4874 | `68c0d14e6d30` | `7acb5502d150` | composition |
| 6 | #4901 | `c1a5e604f5ab` | `1794981605a8` | composition |
| 7 | #4969 | `57502f87f3c1` | `36a84f70b2db` | composition |
| 8 | #4748 | `75da0883382d` | `fe6fdb3f9782` | composition |
| 9 | #4849 | `5e1103cc5014` | `f6750511c6b2` | composition |
| 10 | #4850 | `dabdc06d210a` | `9dd354e299c9` | composition |
| 11 | #4888 | `564417662770` | `9a3adc156d92` | composition |

## Écartés

| lot | motif |
|---|---|
| #4873 | test_2179 rouge, reproduit en local, tête c002abfec394 rouge seule — --exclude, recomposition sans lui |
| #4818 | test_1647, test_4299, test_l85 x2 rouges, bissectés, tête c41222f89dac rouge seule sur main — --exclude, recomposition sans lui |
| #4880 | test_1323 rouge, bissecté, tête 4eb2fb8459c1 rouge seule sur main — --exclude, recomposition sans lui |
| #4397 | test_3379 rouge, bissecté, tête 69f29a0029cb rouge seule sur main — --exclude, recomposition sans lui |
| #4963 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4986 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4975 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4412 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 15 lot(s) prêt(s), 7 en attente, 8 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 21.0 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
