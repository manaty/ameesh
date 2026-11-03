# Paquet integration/paquet-h-20261103-21 — composition (§12), sur main `a26655bd20e1`

Base : `main` **`a26655bd20e161c0499c293cd8cfb01aa27f8634`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-21`, cible `main`.
Porteur : **#5683**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5681 | `fd28e9b3db77` |
| #5680 | `d72a70acf726` |
| #5682 | `2e0aa4f7c6f8` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5681 | `fd28e9b3db77` | `b9f0470858ba` | composition |
| 2 | #5680 | `d72a70acf726` | `5c756b91a515` | composition |
| 3 | #5682 | `2e0aa4f7c6f8` | `8bd3d70eb877` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 41.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
