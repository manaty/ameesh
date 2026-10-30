# Paquet integration/paquet-h-20261030-02 — composition (§12), sur main `eb3ae94f9107`

Base : `main` **`eb3ae94f9107fc51236bf79ee0445c6b645bed53`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-02`, cible `main`.
Porteur : **#4746**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #4736 | `602d0a23a8ee` |
| #4741 | `4d59b84f4b1a` |
| #4409 | `01219c411237` |
| #4576 | `c8f7e2aa4d69` |
| #4729 | `6232cd395c49` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4736 | `602d0a23a8ee` | `63d4d8c51d7c` | composition |
| 2 | #4741 | `4d59b84f4b1a` | `14f532079e53` | composition |
| 3 | #4409 | `01219c411237` | `9c346c974f28` | composition |
| 4 | #4576 | `c8f7e2aa4d69` | `21d544ad3f79` | composition |
| 5 | #4729 | `6232cd395c49` | `94e884bb6e24` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 5 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 22.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
