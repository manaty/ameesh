# Paquet integration/paquet-h-20261030-13 — composition (§12), sur main `0382fd10e903`

Base : `main` **`0382fd10e90325a86a64dfe838341ad253e02e55`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-13`, cible `main`.
Porteur : **#4917**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #4353 | `01adeaa535a9` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4353 | `01adeaa535a9` | `e6ff45d06a9e` | composition |

## Écartés

| lot | motif |
|---|---|
| #4791 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4809 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4899 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4900 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4834 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4868 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4870 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4871 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4880 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4887 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4901 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4888 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |

Liste calculée : 13 lot(s) prêt(s), 5 en attente, 12 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 19.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
