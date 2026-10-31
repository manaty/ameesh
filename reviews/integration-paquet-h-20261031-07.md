# Paquet integration/paquet-h-20261031-07 — composition (§12), sur main `eaaeb411c20f`

Base : `main` **`eaaeb411c20f74ffcdad3c2f74c67cbcbbecbf51`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-07`, cible `main`.
Porteur : **#5075**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5066 | `cff85d7d32c4` |
| #5074 | `0fe8bd6344b8` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5066 | `cff85d7d32c4` | `3cc299a2c673` | composition |
| 2 | #5074 | `0fe8bd6344b8` | `414ce4b817bf` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 20.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
