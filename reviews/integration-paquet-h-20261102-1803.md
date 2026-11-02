# Paquet integration/paquet-h-20261102-1803 — composition (§12), sur main `71120f375adb`

Base : `main` **`71120f375adbc96db8316b820e36fe9c7d4fffe6`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-1803`, cible `main`.
Porteur : **#5531**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5503 | `8df03b13eea7` |
| #5481 | `e8abdf42bec1` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5503 | `8df03b13eea7` | `b66721f63cb1` | composition |
| 2 | #5481 | `e8abdf42bec1` | `5cdf3ab0058b` | composition |

## Écartés

| lot | motif |
|---|---|
| #5529 | conflit, coupure suivante — fichiers : site/docs/concepts/budgets.md, src/ameesh/runner.py |

Liste calculée : 3 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 45.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
