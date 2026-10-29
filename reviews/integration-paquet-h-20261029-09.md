# Paquet integration/paquet-h-20261029-09 — composition (§12), sur main `c253f129603c`

Base : `main` **`c253f129603cf2320ba517b5c2a94632ff949aab`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261029-09`, cible `main`.
Porteur : **#4645**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #4616 | `1f324e0a332f` |
| #4353 | `2931e7ea31ae` |
| #4620 | `fa1d9c12eb1e` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4616 | `1f324e0a332f` | `10c3246c720d` | composition |
| 2 | #4353 | `2931e7ea31ae` | `764a28f9b40d` | composition |
| 3 | #4620 | `fa1d9c12eb1e` | `99894738e476` | composition |

## Écartés

| lot | motif |
|---|---|
| #4610 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4623 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4299 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4384 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4391 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4408 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4463 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4508 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4562 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4574 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4602 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4609 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4552 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4579 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4593 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4605 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |

Liste calculée : 19 lot(s) prêt(s), 2 en attente, 16 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 25.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
