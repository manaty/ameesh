# Paquet integration/paquet-h-20261101-01 — composition (§12), sur main `8d0a53b40748`

Base : `main` **`8d0a53b407488b71349b532cdffba48e0a1e9025`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261101-01`, cible `main`.
Porteur : **#5196**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (15 unités, 15 lots)

| lot | source composée |
|---|---|
| #5151 | `6996143a8f4c` |
| #5162 | `461f0f37b887` |
| #5166 | `c6dde2692a05` |
| #5185 | `93c655c5af66` |
| #5083 | `a581ca410fdc` |
| #5135 | `8dec32b1bba8` |
| #5148 | `7fb38d710fc8` |
| #5152 | `7ec991e35efd` |
| #5161 | `170a9cea16a9` |
| #5175 | `4eccda1d2d27` |
| #5180 | `5f644049e2fc` |
| #5192 | `3268461f6737` |
| #4968 | `d4e31aa971a0` |
| #5027 | `73936af7407d` |
| #5163 | `68166b41f79c` |

## Composition (15 unités, 15 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5151 | `6996143a8f4c` | `fd6b953cb274` | composition |
| 2 | #5162 | `461f0f37b887` | `a69233235960` | composition |
| 3 | #5166 | `c6dde2692a05` | `07c3d6a87b68` | composition |
| 4 | #5185 | `93c655c5af66` | `807f93ed33b6` | composition |
| 5 | #5083 | `a581ca410fdc` | `d9499c4932f9` | composition |
| 6 | #5135 | `8dec32b1bba8` | `5dd9eade1dac` | composition |
| 7 | #5148 | `7fb38d710fc8` | `c95fb217f248` | composition |
| 8 | #5152 | `7ec991e35efd` | `defeca1fcb9a` | composition |
| 9 | #5161 | `170a9cea16a9` | `f0cf89309e55` | composition |
| 10 | #5175 | `4eccda1d2d27` | `31cfa9b1fd92` | composition |
| 11 | #5180 | `5f644049e2fc` | `d7e815ebf8e1` | composition |
| 12 | #5192 | `3268461f6737` | `066a063df725` | composition |
| 13 | #4968 | `d4e31aa971a0` | `2dc0eb170718` | composition |
| 14 | #5027 | `73936af7407d` | `86c5c1a2802f` | composition |
| 15 | #5163 | `68166b41f79c` | `63e68e396a17` | composition |

## Écartés

| lot | motif |
|---|---|
| #5184 | défaut produit réel prouvé par impl-08 (#177161) : shell reparenté avant SESSION_PARENT, session morte non vue ; le test l159b ajouté par 9010ccd78b14 rougit en CI (run 41255225569, tentative 1), vert par chance à la tentative 2. Correctif regelé feb67c4337e7 (avis auditeur en attente) : paquet suivant — --exclude, recomposition sans lui |
| #5189 | tests adaptés, regelé abd766ea0 (rebasé sur main 8d0a53b40), en re-relecture : paquet suivant — --exclude, recomposition sans lui |
| #5179 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5147 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5149 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4902 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4976 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5170 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 21 lot(s) prêt(s), 6 en attente, 8 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 25.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
