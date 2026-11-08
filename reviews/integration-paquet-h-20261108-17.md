# Paquet integration/paquet-h-20261108-17 — composition (§12), sur main `bd8b098d8bc6`

Base : `main` **`bd8b098d8bc6f2b974209e46fab4e96809d0ce3a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261108-17`, cible `main`.
Porteur : **#6348**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6329 | `8c7a9ff18ecb` |
| #6345 | `535a78c1a703` |
| #6334 | `69ead959f67d` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6329 | `8c7a9ff18ecb` | `974c034e8f9d` | composition |
| 2 | #6345 | `535a78c1a703` | `a6bc27b10b0d` | composition |
| 3 | #6334 | `69ead959f67d` | `da7d6b634836` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 69.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
