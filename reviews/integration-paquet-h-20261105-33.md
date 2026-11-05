# Paquet integration/paquet-h-20261105-33 — composition (§12), sur main `33bfa4735ccb`

Base : `main` **`33bfa4735ccb3a4b4c3ee2675a181f743b095eb9`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-33`, cible `main`.
Porteur : **#5990**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #5971 | `d30ffaabb86d` |
| #5980 | `498db2cc5129` |
| #5987 | `c2ce07bf4567` |
| #5981 | `c51d40c84d5c` |
| #5986 | `4ef545837228` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5971 | `d30ffaabb86d` | `701262cac62f` | composition |
| 2 | #5980 | `498db2cc5129` | `24991b5b90cb` | composition |
| 3 | #5987 | `c2ce07bf4567` | `530787372f6c` | composition |
| 4 | #5981 | `c51d40c84d5c` | `eac8aea84a3c` | composition |
| 5 | #5986 | `4ef545837228` | `c390f983a5be` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 5 lot(s) prêt(s), 6 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 40.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
