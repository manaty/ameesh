# Paquet integration/paquet-h-20261110-15 — composition (§12), sur main `df2418cbce2f`

Base : `main` **`df2418cbce2fdb287d5c364250dd1f7ed3e896d8`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-15`, cible `main`.
Porteur : **#6782**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #6761 | `314e87901517` |
| #6765 | `c3acc43aab4c` |
| #6769 | `989ad6c8e78b` |
| #6777 | `7805ee6878cd` |
| #6778 | `22477f7cf856` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6761 | `314e87901517` | `f1fd00da6021` | composition |
| 2 | #6765 | `c3acc43aab4c` | `f7cee17ed803` | composition |
| 3 | #6769 | `989ad6c8e78b` | `d8d74b07fbdd` | composition |
| 4 | #6777 | `7805ee6878cd` | `2a4a6a94dab5` | composition |
| 5 | #6778 | `22477f7cf856` | `71677ad95dd9` | composition |

## Écartés

| lot | motif |
|---|---|
| #6776 | conflit, coupure suivante — fichiers : src/ameesh/staging_executor.py, src/ameesh/staging_watch.py |

Liste calculée : 6 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 52.8 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
