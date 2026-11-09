# Paquet integration/paquet-h-20261109-12 — composition (§12), sur main `6e812ddb8327`

Base : `main` **`6e812ddb8327cc445c82b55c89bb31b25264b66d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261109-12`, cible `main`.
Porteur : **#6526**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6475 | `6227d9e231e1` |
| #6486 | `9f2e72840648` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6475 | `6227d9e231e1` | `89022ea62a08` | composition |
| 2 | #6486 | `9f2e72840648` | `a5db63acfcb8` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 69.0 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
