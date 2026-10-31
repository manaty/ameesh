# Paquet integration/paquet-h-20261031-02 — composition (§12), sur main `9306a60cac47`

Base : `main` **`9306a60cac478b38a7e2c5b373b95c977573ca33`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-02`, cible `main`.
Porteur : **#5036**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (9 unités, 9 lots)

| lot | source composée |
|---|---|
| #5030 | `090e86a33e97` |
| #4944 | `6118b89bedb0` |
| #4970 | `984815e67fc7` |
| #5006 | `1aae2d527abd` |
| #5022 | `8b1f10a819da` |
| #4410 | `a0f7953bed10` |
| #4882 | `d5e812f342a5` |
| #4892 | `39e810ffaf9d` |
| #5016 | `3d4b29403d19` |

## Composition (9 unités, 9 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5030 | `090e86a33e97` | `7fb13dd5d4d1` | composition |
| 2 | #4944 | `6118b89bedb0` | `453695ceefe4` | composition |
| 3 | #4970 | `984815e67fc7` | `8c36fe57d4b1` | composition |
| 4 | #5006 | `1aae2d527abd` | `ca7a3230b664` | composition |
| 5 | #5022 | `8b1f10a819da` | `acc33d2ca20e` | composition |
| 6 | #4410 | `a0f7953bed10` | `174bdd4d996b` | composition |
| 7 | #4882 | `d5e812f342a5` | `e0dda11b9641` | composition |
| 8 | #4892 | `39e810ffaf9d` | `a4ba91a13810` | composition |
| 9 | #5016 | `3d4b29403d19` | `bf8ac81b8717` | composition |

## Écartés

| lot | motif |
|---|---|
| #4989 | test_4986 rouge sur 79d2e84ee160, KeyError controller_key (run 41152634546), corrigé sur 901ed42e76f7, en attente de vérification du delta — --exclude, recomposition sans lui |
| #5034 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4975 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5010 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5028 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5029 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5035 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4964 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 16 lot(s) prêt(s), 5 en attente, 8 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 22.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
