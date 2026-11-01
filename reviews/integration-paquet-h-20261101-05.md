# Paquet integration/paquet-h-20261101-05 — composition (§12), sur main `83a1862237bf`

Base : `main` **`83a1862237bfebbfe76a257155fcee45de2f82a7`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261101-05`, cible `main`.
Porteur : **#5226**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #5225 | `23d456372bf6` |
| #5213 | `06eb95c88f5c` |
| #5214 | `55421995c5b2` |
| #5217 | `3314cd2a6bbf` |
| #4553 | `4a8244c6be46` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5225 | `23d456372bf6` | `52aa2e632582` | composition |
| 2 | #5213 | `06eb95c88f5c` | `66854e07aefd` | composition |
| 3 | #5214 | `55421995c5b2` | `59925d37064d` | composition |
| 4 | #5217 | `3314cd2a6bbf` | `e46042931e66` | composition |
| 5 | #4553 | `4a8244c6be46` | `c13f9d500733` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 5 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 25.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
