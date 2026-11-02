# Paquet integration/paquet-h-20261102-1804 — composition (§12), sur main `71120f375adb`

Base : `main` **`71120f375adbc96db8316b820e36fe9c7d4fffe6`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-1804`, cible `main`.
Porteur : **#5532**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5481 | `e8abdf42bec1` |
| #5509 | `71c45c93c4dc` |
| #5520 | `2efdfa93891b` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5481 | `e8abdf42bec1` | `6f7a65ab02f9` | composition |
| 2 | #5509 | `71c45c93c4dc` | `7e36d8801e07` | composition |
| 3 | #5520 | `2efdfa93891b` | `9c17cda70b5c` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 37.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
