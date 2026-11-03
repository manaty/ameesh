# Paquet integration/paquet-h-20261103-09 — composition (§12), sur main `6af6364c0b41`

Base : `main` **`6af6364c0b415593083832cb193b7203fb3b62bc`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-09`, cible `main`.
Porteur : **#5578**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5571 | `d08f3b8041c0` |
| #5568 | `4957a0788ed1` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5571 | `d08f3b8041c0` | `3aedc5eda5c3` | composition |
| 2 | #5568 | `4957a0788ed1` | `0d218fae1165` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 47.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
