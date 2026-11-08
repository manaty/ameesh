# Paquet integration/paquet-h-20261108-15 — composition (§12), sur main `0502611ace41`

Base : `main` **`0502611ace41a6fc1253e3d957c0a38b26d86e4d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261108-15`, cible `main`.
Porteur : **#6331**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6313 | `bab6dfffd404` |
| #6327 | `7bbf3820e14c` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6313 | `bab6dfffd404` | `ecc5a7fcce6e` | composition |
| 2 | #6327 | `7bbf3820e14c` | `0e6890f762e5` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 46.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
