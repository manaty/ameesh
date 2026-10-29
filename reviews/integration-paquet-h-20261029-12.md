# Paquet integration/paquet-h-20261029-12 — composition (§12), sur main `ba41e3bb6282`

Base : `main` **`ba41e3bb6282801c4542a86a10df2ee8bec7cd20`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261029-12`, cible `main`.
Porteur : **#4690**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (10 unités, 10 lots)

| lot | source composée |
|---|---|
| #4623 | `9ea1203c6331` |
| #4685 | `54d1ccfb73f4` |
| #4385 | `78c6f44a06d0` |
| #4391 | `28f66c8f7823` |
| #4408 | `33c7bb37c1f1` |
| #4515 | `7c12092fc8c8` |
| #4574 | `5c70627af45b` |
| #4673 | `0e73f4d01a5a` |
| #4682 | `abe90a8ad9b7` |
| #4567 | `34058a538628` |

## Composition (10 unités, 10 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4623 | `9ea1203c6331` | `1a460c44df44` | composition |
| 2 | #4685 | `54d1ccfb73f4` | `25de308c44af` | composition |
| 3 | #4385 | `78c6f44a06d0` | `efca6061b573` | composition |
| 4 | #4391 | `28f66c8f7823` | `dae8d4dedb9d` | composition |
| 5 | #4408 | `33c7bb37c1f1` | `d9fce6ce0f3c` | composition |
| 6 | #4515 | `7c12092fc8c8` | `e59b930498cd` | composition |
| 7 | #4574 | `5c70627af45b` | `4afb488806f3` | composition |
| 8 | #4673 | `0e73f4d01a5a` | `913edf4573c8` | composition |
| 9 | #4682 | `abe90a8ad9b7` | `231e8be68acb` | composition |
| 10 | #4567 | `34058a538628` | `597a045c6eb7` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 10 lot(s) prêt(s), 5 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 26.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
