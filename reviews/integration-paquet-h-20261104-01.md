# Paquet integration/paquet-h-20261104-01 — composition (§12), sur main `cd0bfc12d0a5`

Base : `main` **`cd0bfc12d0a5c56078cc696498bdf4e4da3a6010`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-01`, cible `main`.
Porteur : **#5704**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5688 | `5ea2d794a53c` |
| #5697 | `117f396cfb6a` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5688 | `5ea2d794a53c` | `512293632586` | composition |
| 2 | #5697 | `117f396cfb6a` | `49fd32bf8c4a` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 6 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 46.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
