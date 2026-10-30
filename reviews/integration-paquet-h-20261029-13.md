# Paquet integration/paquet-h-20261029-13 — composition (§12), sur main `44fac6f585d4`

Base : `main` **`44fac6f585d491217e6f382459cdf3248589e34d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261029-13`, cible `main`.
Porteur : **#4701**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #4562 | `a4c2e45dab30` |
| #4609 | `b1492d0d3150` |
| #4687 | `35960df6304d` |
| #4692 | `e4051e1f7f1f` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4562 | `a4c2e45dab30` | `33794297490b` | composition |
| 2 | #4609 | `b1492d0d3150` | `0fa353ec0ab6` | composition |
| 3 | #4687 | `35960df6304d` | `1add42406667` | composition |
| 4 | #4692 | `e4051e1f7f1f` | `289ee4230b9e` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 9 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 23.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
