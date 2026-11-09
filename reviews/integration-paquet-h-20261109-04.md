# Paquet integration/paquet-h-20261109-04 — composition (§12), sur main `a20ab7eea189`

Base : `main` **`a20ab7eea189e6b9a070d4d1ee1df6ed8aa59b38`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261109-04`, cible `main`.
Porteur : **#6438**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #6422 | `85dc461693cd` |
| #6428 | `3a99da56488d` |
| #6425 | `6b6c72d4edd1` |
| #6426 | `34bbdd1dd8b5` |
| #6427 | `c68b32177183` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6422 | `85dc461693cd` | `5f630c419574` | composition |
| 2 | #6428 | `3a99da56488d` | `cfa2e2f13242` | composition |
| 3 | #6425 | `6b6c72d4edd1` | `8a18bd970a8e` | composition |
| 4 | #6426 | `34bbdd1dd8b5` | `912714757c32` | composition |
| 5 | #6427 | `c68b32177183` | `7f4e2a627a61` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 5 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 67.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
