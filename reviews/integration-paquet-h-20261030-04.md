# Paquet integration/paquet-h-20261030-04 — composition (§12), sur main `93c8fd742da5`

Base : `main` **`93c8fd742da5bf46d8d8051f18fb8549ab922b51`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-04`, cible `main`.
Porteur : **#4771**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #4750 | `cb044395d632` |
| #4754 | `2cb7122ee95c` |
| #4758 | `49bd639acccb` |
| #4617 | `28b44c07f343` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4750 | `cb044395d632` | `3f734e5006f5` | composition |
| 2 | #4754 | `2cb7122ee95c` | `52ebb43fcf6d` | composition |
| 3 | #4758 | `49bd639acccb` | `354699c51b98` | composition |
| 4 | #4617 | `28b44c07f343` | `e0a29c831a90` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 5 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 20.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
