# Paquet integration/paquet-h-20261112-08 — composition (§12), sur main `7e109c1471a9`

Base : `main` **`7e109c1471a93bc33bf7cf7b55c00a5ec180133f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261112-08`, cible `main`.
Porteur : **#7114**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #6978 | `aaa23769e2a3` |
| #7109 | `e102d407fcfb` |
| #6992 | `8ee998733ad8` |
| #7013 | `356e6a877fab` |
| #7058 | `1d7e41d56478` |
| #7084 | `6eb636767733` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6978 | `aaa23769e2a3` | `996c8a67ad7f` | composition |
| 2 | #7109 | `e102d407fcfb` | `2dd9b1f6c943` | composition |
| 3 | #6992 | `8ee998733ad8` | `532d2f7fd874` | composition |
| 4 | #7013 | `356e6a877fab` | `cca901bbab88` | composition |
| 5 | #7058 | `1d7e41d56478` | `2a800cc82675` | composition |
| 6 | #7084 | `6eb636767733` | `3301c69eee0c` | composition |

## Écartés

| lot | motif |
|---|---|
| #6984 | rouge CI réel (run 43282073678, psycopg 9/12) : test_3557 FloorCompletionTest.test_a_removed_partial_seat_stays_out_on_an_unavailable_retry ; reproduit seul sur la tête gelée ab07b2b2cc54 et à l'ajout de #6984 dans la composition (e03512fdf), vert sur main et avant (mesh-design) — --exclude, recomposition sans lui |
| #6990 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7004 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6861 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6893 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7048 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6856 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7116 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 13 lot(s) prêt(s), 6 en attente, 8 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 47.7 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
