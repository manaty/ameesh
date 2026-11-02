# Paquet integration/paquet-h-20261102-2100 — composition (§12), sur main `71120f375adb`

Base : `main` **`71120f375adbc96db8316b820e36fe9c7d4fffe6`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-2100`, cible `main`.
Porteur : **#5535**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5529 | `b313d536c785` |
| #5509 | `71c45c93c4dc` |
| #5520 | `2efdfa93891b` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5529 | `b313d536c785` | `73b7cbab408a` | composition |
| 2 | #5509 | `71c45c93c4dc` | `310a1a0b7876` | composition |
| 3 | #5520 | `2efdfa93891b` | `a5a91e5e4d9e` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 36.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
