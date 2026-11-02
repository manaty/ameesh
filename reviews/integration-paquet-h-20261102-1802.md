# Paquet integration/paquet-h-20261102-1802 — composition (§12), sur main `b4e980783f20`

Base : `main` **`b4e980783f20d15e8c67b614c96015175fe469a2`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-1802`, cible `main`.
Porteur : **#5526**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #5493 | `996ca2945734` |
| #5499 | `417e00739c7c` |
| #5502 | `b183a67e2902` |
| #5506 | `477a6704bbf5` |
| #5517 | `b0315930bcf0` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5493 | `996ca2945734` | `f17f29039017` | composition |
| 2 | #5499 | `417e00739c7c` | `62856a7ff135` | composition |
| 3 | #5502 | `b183a67e2902` | `6258850b0c00` | composition |
| 4 | #5506 | `477a6704bbf5` | `d14765e553fe` | composition |
| 5 | #5517 | `b0315930bcf0` | `1808d1a8115b` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 5 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 39.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
