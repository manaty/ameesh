# Paquet integration/paquet-h-20261109-05 — composition (§12), sur main `6dd9eb59ebee`

Base : `main` **`6dd9eb59ebee5da8c22a0cd51b6cb2d8a121406a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261109-05`, cible `main`.
Porteur : **#6443**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6437 | `063e45668dd9` |
| #6424 | `df5b8e53df82` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6437 | `063e45668dd9` | `3c9fcfd7044e` | composition |
| 2 | #6424 | `df5b8e53df82` | `ad4154d30146` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 53.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
