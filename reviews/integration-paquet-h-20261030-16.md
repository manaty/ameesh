# Paquet integration/paquet-h-20261030-16 — composition (§12), sur main `4fdcfe4df6ec`

Base : `main` **`4fdcfe4df6ec44c95bc21b59192951dca6bff421`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-16`, cible `main`.
Porteur : **#4953**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #4353 | `38e641e998c5` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4353 | `38e641e998c5` | `10072e0eba43` | composition |

## Écartés

| lot | motif |
|---|---|
| #4868 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4870 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4880 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4901 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |
| #4888 | paquet parallèle : autre groupe disjoint — --only, second paquet parallèle (#3256 C) |

Liste calculée : 6 lot(s) prêt(s), 6 en attente, 5 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 22.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
