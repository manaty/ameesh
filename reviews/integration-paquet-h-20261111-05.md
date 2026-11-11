# Paquet integration/paquet-h-20261111-05 — composition (§12), sur main `e7418616c7e8`

Base : `main` **`e7418616c7e82f9441d05a615c27d152b76c99fa`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-05`, cible `main`.
Porteur : **#6823**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6814 | `e1d1842ed647` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6814 | `e1d1842ed647` | `dc74b92c389a` | composition |

## Écartés

| lot | motif |
|---|---|
| #6806 | coupure suivante : membre non prêt — composé 943fd7a39f7c, plus dans la liste prête |
| #6808 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 2 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 52.8 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
