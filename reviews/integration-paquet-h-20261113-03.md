# Paquet integration/paquet-h-20261113-03 — composition (§12), sur main `31b5063af87f`

Base : `main` **`31b5063af87f822442f3345c3137cedfaa05243d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-03`, cible `main`.
Porteur : **#7168**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (13 unités, 13 lots)

| lot | source composée |
|---|---|
| #6862 | `10459b38f665` |
| #6981 | `c9e058bee075` |
| #6984 | `01a0795bca2a` |
| #7007 | `49567e808b7e` |
| #7019 | `cf3bd2703b4f` |
| #7050 | `bc4c1e4e25f8` |
| #7119 | `b95b6ad31e88` |
| #7163 | `c347951e436b` |
| #6982 | `e1b5838c0678` |
| #6993 | `922e91db83eb` |
| #7012 | `181e94a2378b` |
| #7015 | `222d1c9e265f` |
| #7161 | `3b157ea3a834` |

## Composition (13 unités, 13 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6862 | `10459b38f665` | `867e00363f99` | composition |
| 2 | #6981 | `c9e058bee075` | `cf69664bcb71` | composition |
| 3 | #6984 | `01a0795bca2a` | `f0deed20e0e8` | composition |
| 4 | #7007 | `49567e808b7e` | `34a92c3e22d5` | composition |
| 5 | #7019 | `cf3bd2703b4f` | `49197c755121` | composition |
| 6 | #7050 | `bc4c1e4e25f8` | `9ecea7bb056b` | composition |
| 7 | #7119 | `b95b6ad31e88` | `007ee5593693` | composition |
| 8 | #7163 | `c347951e436b` | `96bf09fc9f14` | composition |
| 9 | #6982 | `e1b5838c0678` | `91d1aa4dfa0a` | composition |
| 10 | #6993 | `922e91db83eb` | `bc877d1c6721` | composition |
| 11 | #7012 | `181e94a2378b` | `cb0ad195400a` | composition |
| 12 | #7015 | `222d1c9e265f` | `852cb2d345db` | composition |
| 13 | #7161 | `3b157ea3a834` | `9ac6639ce161` | composition |

## Écartés

| lot | motif |
|---|---|
| #7106 | rouges reproduits en local (2167, 2339, 1425, 2179, 4299) ; la tête regelée 4e4f6f47696f échoue encore 2179 et 4299 — --exclude, recomposition sans lui |
| #6961 | test_l226b_auth_failure.UnpacedChoiceTest (2) vert sur main 31b5063af87f, rouge juste après l'unité #6961 — --exclude, recomposition sans lui |
| #7149 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7164 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 15 lot(s) prêt(s), 2 en attente, 4 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 44.9 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
