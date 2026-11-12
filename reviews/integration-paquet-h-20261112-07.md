# Paquet integration/paquet-h-20261112-07 — composition (§12), sur main `e5e08e2f47f3`

Base : `main` **`e5e08e2f47f3158a382c45881b52a80c533ae4ba`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261112-07`, cible `main`.
Porteur : **#7103**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (9 unités, 9 lots)

| lot | source composée |
|---|---|
| #6873 | `dbd8d95d3100` |
| #6972 | `ab7939b87d96` |
| #6867 | `578ad2ffd091` |
| #6933 | `3692faa8ee54` |
| #6960 | `71105bafef86` |
| #7000 | `e851bb018352` |
| #7081 | `996b14666874` |
| #7094 | `afce6fbeeaa3` |
| #7062 | `93e8bbd8c8a3` |

## Composition (9 unités, 9 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6873 | `dbd8d95d3100` | `cab42ee50073` | composition |
| 2 | #6972 | `ab7939b87d96` | `d440af41a71b` | composition |
| 3 | #6867 | `578ad2ffd091` | `6cfbd23b554b` | composition |
| 4 | #6933 | `3692faa8ee54` | `13a7186b53d9` | composition |
| 5 | #6960 | `71105bafef86` | `9018f5d96b92` | composition |
| 6 | #7000 | `e851bb018352` | `0cb869999f23` | composition |
| 7 | #7081 | `996b14666874` | `613d0cd34bdb` | composition |
| 8 | #7094 | `afce6fbeeaa3` | `09cef0e69569` | composition |
| 9 | #7062 | `93e8bbd8c8a3` | `ec1777792b22` | composition |

## Écartés

| lot | motif |
|---|---|
| #7048 | B réel en CI : migration 0570 sans marqueur constraint not tightened (test_migrations_additives) ; regelé, attend ses avis — --exclude, recomposition sans lui |
| #6978 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7109 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6984 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6992 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7013 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7050 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7058 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 16 lot(s) prêt(s), 9 en attente, 8 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 52.7 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
