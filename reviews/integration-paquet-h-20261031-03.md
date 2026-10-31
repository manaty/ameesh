# Paquet integration/paquet-h-20261031-03 — composition (§12), sur main `49e959046dad`

Base : `main` **`49e959046dad09d5d97abcf6038e03fd7d82d028`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-03`, cible `main`.
Porteur : **#5049**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (8 unités, 8 lots)

| lot | source composée |
|---|---|
| #5000 | `df5284f324c9` |
| #5034 | `982c908e0d15` |
| #4989 | `45b8e9c9308d` |
| #5010 | `f353e376f781` |
| #5028 | `18f518787829` |
| #5035 | `8a14691f9818` |
| #5046 | `2b5afa835591` |
| #4964 | `1d44a29aaa17` |

## Composition (8 unités, 8 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5000 | `df5284f324c9` | `06eea3704dbc` | composition |
| 2 | #5034 | `982c908e0d15` | `a51a8970d66d` | composition |
| 3 | #4989 | `45b8e9c9308d` | `62cf644436d6` | composition |
| 4 | #5010 | `f353e376f781` | `8dbda4d2b50c` | composition |
| 5 | #5028 | `18f518787829` | `61e9ed6a9c4a` | composition |
| 6 | #5035 | `8a14691f9818` | `a8ef6b5d2cd5` | composition |
| 7 | #5046 | `2b5afa835591` | `571fcc826d97` | composition |
| 8 | #4964 | `1d44a29aaa17` | `82998ee134ad` | composition |

## Écartés

| lot | motif |
|---|---|
| #4975 | 8 échecs de contrat sur e801cd6b899d (run 41160212297), corrigés sur 47ba0c55a9c3, hors de cette recomposition — --exclude, recomposition sans lui |
| #4781 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4818 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5015 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5026 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5029 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5042 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5043 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5044 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5045 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5052 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 18 lot(s) prêt(s), 3 en attente, 11 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 22.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
