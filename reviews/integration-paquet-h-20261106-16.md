# Paquet integration/paquet-h-20261106-16 — composition (§12), sur main `73fcd49b0e52`

Base : `main` **`73fcd49b0e526775aa73564cf4c85a293196b021`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261106-16`, cible `main`.
Porteur : **#6058**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6054 | `0a1914c38fb5` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6054 | `0a1914c38fb5` | `c4ff4d0d88da` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 42.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
