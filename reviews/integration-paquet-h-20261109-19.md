# Paquet integration/paquet-h-20261109-19 — composition (§12), sur main `dfe101bcbe71`

Base : `main` **`dfe101bcbe714cb900fa30e26a67fcea1442e1f8`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261109-19`, cible `main`.
Porteur : **#6573**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6485 | `2c6bd6d31644` |
| #6563 | `cb85f3d27d09` |
| #6568 | `7d738ce9cfaf` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6485 | `2c6bd6d31644` | `8050a2f5bfee` | composition |
| 2 | #6563 | `cb85f3d27d09` | `541178eee654` | composition |
| 3 | #6568 | `7d738ce9cfaf` | `865301e5d277` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 47.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
