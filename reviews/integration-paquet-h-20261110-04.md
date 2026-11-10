# Paquet integration/paquet-h-20261110-04 — composition (§12), sur main `f448c85b4898`

Base : `main` **`f448c85b4898bbb45d110951135a373c864f5b8b`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-04`, cible `main`.
Porteur : **#6634**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6609 | `9527d1e0134d` |
| #6630 | `2a19b2a9eab5` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6609 | `9527d1e0134d` | `bf53f5e89e78` | composition |
| 2 | #6630 | `2a19b2a9eab5` | `72bf54b8f3d1` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 53.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
