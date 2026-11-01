# Paquet integration/paquet-h-20261101-14 — composition (§12), sur main `cc134a113951`

Base : `main` **`cc134a11395170ee5c78e8af74e3d03159369aaf`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261101-14`, cible `main`.
Porteur : **#5335**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5327 | `6b85bcf53cfc` |
| #5328 | `af8132774074` |
| #5331 | `3430e4211a66` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5327 | `6b85bcf53cfc` | `5238b7f19cbb` | composition |
| 2 | #5328 | `af8132774074` | `f12fcb66a768` | composition |
| 3 | #5331 | `3430e4211a66` | `bdc3cf7eccf4` | composition |

## Écartés

| lot | motif |
|---|---|
| #5325 | coupure suivante : membre non prêt — composé f9a1660ba99b, plus dans la liste prête |

Liste calculée : 3 lot(s) prêt(s), 2 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 24.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
