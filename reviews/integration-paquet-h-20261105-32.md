# Paquet integration/paquet-h-20261105-32 — composition (§12), sur main `7ddf5e15c7f7`

Base : `main` **`7ddf5e15c7f798a8a4278d86b829305ae627ece9`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-32`, cible `main`.
Porteur : **#5988**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5976 | `2e188ca84837` |
| #5982 | `7ba8d1f5186f` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5976 | `2e188ca84837` | `de6d7472bb31` | composition |
| 2 | #5982 | `7ba8d1f5186f` | `5de596b003a6` | composition |

## Écartés

| lot | motif |
|---|---|
| #5979 | conflit, coupure suivante — fichiers : src/ameesh/scoped_db.py |
| #5981 | conflit, coupure suivante — fichiers : src/ameesh/agent_peek.py |

Liste calculée : 4 lot(s) prêt(s), 5 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 42.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
