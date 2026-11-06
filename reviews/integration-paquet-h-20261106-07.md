# Paquet integration/paquet-h-20261106-07 — composition (§12), sur main `26e02f68ce9f`

Base : `main` **`26e02f68ce9fd504a5da899fa3e7bbe9d409c1e3`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261106-07`, cible `main`.
Porteur : **#6025**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6010 | `5c434b0f244d` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6010 | `5c434b0f244d` | `8f87afcb6735` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 5 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 42.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
