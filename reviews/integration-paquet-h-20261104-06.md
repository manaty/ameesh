# Paquet integration/paquet-h-20261104-06 — composition (§12), sur main `1341415eca39`

Base : `main` **`1341415eca3948e6203264b252046a0514303246`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-06`, cible `main`.
Porteur : **#5728**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5690 | `e84de5a7a01e` |
| #5722 | `58304494ad92` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5690 | `e84de5a7a01e` | `2caac2ff5d76` | composition |
| 2 | #5722 | `58304494ad92` | `fcf6b60fca9c` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 37.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
