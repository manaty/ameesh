# Paquet integration/paquet-h-20261111-11 — composition (§12), sur main `2d206ca7596f`

Base : `main` **`2d206ca7596f4e5bcfc989d4c665fa43be4c4e26`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-11`, cible `main`.
Porteur : **#6864**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6834 | `c4415f2d3df7` |
| #6853 | `c84b8106efdb` |
| #6854 | `f466c6bc2bb1` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6834 | `c4415f2d3df7` | `213b20b6a539` | composition |
| 2 | #6853 | `c84b8106efdb` | `dbf85b39927f` | composition |
| 3 | #6854 | `f466c6bc2bb1` | `b4a4abb80b88` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 49.7 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
