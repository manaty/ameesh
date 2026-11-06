# Paquet integration/paquet-h-20261106-19 — composition (§12), sur main `65fad87eb500`

Base : `main` **`65fad87eb500d8ecf32d5a72d8ab25251dbcfd49`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261106-19`, cible `main`.
Porteur : **#6068**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6061 | `06f90ffea5d7` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6061 | `06f90ffea5d7` | `9509a141301b` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 50.0 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
