# Paquet integration/paquet-h-20261030-11 — composition (§12), sur main `26e8e41c90f3`

Base : `main` **`26e8e41c90f39abf4daf24f710afab3a9da6555c`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-11`, cible `main`.
Porteur : **#4884**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #4822 | `47b5f4c01a08` |
| #4831 | `9d97096a66e2` |
| #4869 | `97d30276cae9` |
| #4851 | `c549c1d19e4f` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4822 | `47b5f4c01a08` | `20ed36da839b` | composition |
| 2 | #4831 | `9d97096a66e2` | `d5901ba52d05` | composition |
| 3 | #4869 | `97d30276cae9` | `757533633c62` | composition |
| 4 | #4851 | `c549c1d19e4f` | `e32cfee67694` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 11 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 23.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
