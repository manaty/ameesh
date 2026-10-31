# Paquet integration/paquet-h-20261031-19 — composition (§12), sur main `c1e03782475f`

Base : `main` **`c1e03782475fcad5e3733fef31ec71c243f38639`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-19`, cible `main`.
Porteur : **#5178**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (11 unités, 11 lots)

| lot | source composée |
|---|---|
| #5165 | `480befe3d4f0` |
| #5168 | `c4392943920d` |
| #4972 | `7cb45707bca5` |
| #5041 | `a01883cb2eaa` |
| #5101 | `1a4efd73e7db` |
| #5134 | `4964ab36d052` |
| #5154 | `7165f56ec9c2` |
| #5025 | `070d27dcb7f9` |
| #5051 | `b35050ecedf3` |
| #5136 | `0db424ddca84` |
| #5159 | `552e7b639707` |

## Composition (11 unités, 11 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5165 | `480befe3d4f0` | `d1a369eb625f` | composition |
| 2 | #5168 | `c4392943920d` | `e0085320144f` | composition |
| 3 | #4972 | `7cb45707bca5` | `e6ab1b80fc5c` | composition |
| 4 | #5041 | `a01883cb2eaa` | `88977a12fed4` | composition |
| 5 | #5101 | `1a4efd73e7db` | `1b34f95a1f86` | composition |
| 6 | #5134 | `4964ab36d052` | `4052f099d1a1` | composition |
| 7 | #5154 | `7165f56ec9c2` | `2a13d2fc4fd9` | composition |
| 8 | #5025 | `070d27dcb7f9` | `810d5cea935e` | composition |
| 9 | #5051 | `b35050ecedf3` | `1994f226d388` | composition |
| 10 | #5136 | `0db424ddca84` | `f4b00f7a8e8c` | composition |
| 11 | #5159 | `552e7b639707` | `311bccf2174b` | composition |

## Écartés

| lot | motif |
|---|---|
| #5162 | rouge ci-full 41242761113 (psycopg 8/8) : test_5162 RealIdentityTest, pg_restore 17 contre serveur < 17 du runner (transaction_timeout inconnu) ; vert en local (12 tests) : test dépendant de l'environnement, correctif test demandé à cx1, retour au paquet suivant — --exclude, recomposition sans lui |
| #5151 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5166 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5083 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5148 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5175 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4968 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5027 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 18 lot(s) prêt(s), 5 en attente, 8 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 34.0 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
