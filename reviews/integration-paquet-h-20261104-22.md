# Paquet integration/paquet-h-20261104-22 — composition (§12), sur main `146f70b2a54f`

Base : `main` **`146f70b2a54fe800bf0b6308f6655983aefc45a5`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-22`, cible `main`.
Porteur : **#5803**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #5797 | `f421d4f8305d` |
| #5783 | `51bf2d9a82cb` |
| #5787 | `1f7e361be20a` |
| #5794 | `56f3c1e43325` |
| #5786 | `9a7c251df98e` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5797 | `f421d4f8305d` | `48a60ef3fa63` | composition |
| 2 | #5783 | `51bf2d9a82cb` | `0a297537b6d0` | composition |
| 3 | #5787 | `1f7e361be20a` | `5ff07a7bcc4f` | composition |
| 4 | #5794 | `56f3c1e43325` | `e0601367b540` | composition |
| 5 | #5786 | `9a7c251df98e` | `dbfa73541c9e` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 5 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 44.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
