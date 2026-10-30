# Paquet integration/paquet-h-20261030-19 — composition (§12), sur main `fc891b704331`

Base : `main` **`fc891b704331488fc7b10b232084f3987378eb98`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-19`, cible `main`.
Porteur : **#4999**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #4873 | `ccaf43a9bd94` |
| #4885 | `8cf61127cf1f` |
| #4991 | `5452c452612d` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4873 | `ccaf43a9bd94` | `e4d73ff778e3` | composition |
| 2 | #4885 | `8cf61127cf1f` | `b03557a6fa57` | composition |
| 3 | #4991 | `5452c452612d` | `c66fefca8d25` | composition |

## Écartés

| lot | motif |
|---|---|
| #4898 | conflit, coupure suivante — fichiers : src/ameesh/staging_executor.py, tests/test_4880_staging_executor.py |

Liste calculée : 4 lot(s) prêt(s), 5 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 23.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
