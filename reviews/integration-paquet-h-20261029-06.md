# Paquet integration/paquet-h-20261029-06 — composition (§12), sur main `eddc1d7e230f`

Base : `main` **`eddc1d7e230f59c73bd7b69cd4d7cae1fabdbc22`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261029-06`, cible `main`.
Porteur : **#4548**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (20 unités, 20 lots)

| lot | source composée |
|---|---|
| #4518 | `128b7b42cd3e` |
| #4546 | `6a75a5884b56` |
| #4221 | `b8864bbc4fd0` |
| #4344 | `4752d2cae94c` |
| #4357 | `d905f3ebd6fe` |
| #4383 | `9d8147c99e02` |
| #4425 | `1dfac51be3ad` |
| #4457 | `71df0be82323` |
| #4506 | `be48e94bc662` |
| #4525 | `2240926c4956` |
| #4528 | `cda2cf9654c1` |
| #4533 | `1db2b29a7522` |
| #4549 | `c5e799c0a17e` |
| #4560 | `d4b412a2c712` |
| #4561 | `4e39449c87df` |
| #4455 | `39db9444a287` |
| #4499 | `ad329ffc6051` |
| #4517 | `41ab77d07767` |
| #4519 | `e975d952aaa7` |
| #4526 | `bb820994e175` |

## Composition (20 unités, 20 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4518 | `128b7b42cd3e` | `0482fe70e366` | composition |
| 2 | #4546 | `6a75a5884b56` | `e365e2e609aa` | composition |
| 3 | #4221 | `b8864bbc4fd0` | `e6d1bb618892` | composition |
| 4 | #4344 | `4752d2cae94c` | `34b3ac4a2544` | composition |
| 5 | #4357 | `d905f3ebd6fe` | `bc48eae73616` | composition |
| 6 | #4383 | `9d8147c99e02` | `b3984e66b220` | composition |
| 7 | #4425 | `1dfac51be3ad` | `28c30ddd22f1` | composition |
| 8 | #4457 | `71df0be82323` | `fbd9438195f6` | composition |
| 9 | #4506 | `be48e94bc662` | `2a25d0fafa49` | composition |
| 10 | #4525 | `2240926c4956` | `7407bec884e5` | composition |
| 11 | #4528 | `cda2cf9654c1` | `b1a7b729b832` | composition |
| 12 | #4533 | `1db2b29a7522` | `a6843462a4cd` | composition |
| 13 | #4549 | `c5e799c0a17e` | `1afcb31c2efd` | composition |
| 14 | #4560 | `d4b412a2c712` | `c1b92def76d0` | composition |
| 15 | #4561 | `4e39449c87df` | `9c9b4be65adf` | composition |
| 16 | #4455 | `39db9444a287` | `f79bc0f1164b` | composition |
| 17 | #4499 | `ad329ffc6051` | `22e29010daa4` | composition |
| 18 | #4517 | `41ab77d07767` | `a83daeb4cb17` | composition |
| 19 | #4519 | `e975d952aaa7` | `168323908ec8` | composition |
| 20 | #4526 | `bb820994e175` | `2194c3c40d7f` | composition |

## Écartés

| lot | motif |
|---|---|
| #4571 | test négatif en ci-full 40887503886 : test_undeliverable.test_humain_declare_n_est_pas_une_boite_morte (#4519), filtre SQL human: de dead_letters qui écarte aussi les humains non déclarés — --exclude, recomposition sans lui |
| #4384 | conflit, coupure suivante — fichiers : tests/test_generic_core.py, tests/test_l100_dev_aliases.py |
| #4463 | conflit, coupure suivante — fichiers : scripts/paquet-auto.py |
| #4508 | conflit, coupure suivante — fichiers : src/ameesh/plan_git.py |
| #4562 | conflit, coupure suivante — fichiers : src/ameesh/plan_git.py |
| #4505 | conflit, coupure suivante — fichiers : src/ameesh/storage/postgres/provenance.py, src/ameesh/storage/provenance.py |

Liste calculée : 25 lot(s) prêt(s), 7 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 24.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
