# Paquet integration/paquet-h-20261104-10 — composition (§12), sur main `3b4b9c688a5f`

Base : `main` **`3b4b9c688a5f72edd40f930ce09346d8d5d7a3bc`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-10`, cible `main`.
Porteur : **#5739**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5737 | `ff17cc6f6211` |
| #5734 | `7595ce1fe86d` |
| #5735 | `ecede24c7588` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5737 | `ff17cc6f6211` | `71d060137918` | composition |
| 2 | #5734 | `7595ce1fe86d` | `6598cec9d9dd` | composition |
| 3 | #5735 | `ecede24c7588` | `17745b0a3869` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 43.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
