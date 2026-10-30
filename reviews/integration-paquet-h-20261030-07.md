# Paquet integration/paquet-h-20261030-07 — composition (§12), sur main `4f7fa70c5f37`

Base : `main` **`4f7fa70c5f37a36d33a3d524a0cc02b3688e1873`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-07`, cible `main`.
Porteur : **#4797**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #4770 | `ac9d3d3dacb3` |
| #4516 | `4b46f8086b4c` |
| #4542 | `2ee9312a41d3` |
| #4740 | `2617b977547b` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4770 | `ac9d3d3dacb3` | `a4869019988d` | composition |
| 2 | #4516 | `4b46f8086b4c` | `8d3a4683ef92` | composition |
| 3 | #4542 | `2ee9312a41d3` | `bbdb8c851da7` | composition |
| 4 | #4740 | `2617b977547b` | `9838469b0bef` | composition |

## Écartés

| lot | motif |
|---|---|
| #4779 | rouge réel : test_4610_sleep_work_mail (9), test_4619_panier_4610 (7), test_2776_journal_designations ; reproduit en local au préfixe #4779 (0627f5eab), vert avant — --exclude, recomposition sans lui |
| #4788 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4793 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 6 lot(s) prêt(s), 6 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 25.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
