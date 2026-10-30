# Paquet integration/paquet-h-20261030-01 — composition (§12), sur main `27c5e12933ce`

Base : `main` **`27c5e12933ce5f981596bc267e9d8a698a85f151`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-01`, cible `main`.
Porteur : **#4739**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #4717 | `239b95d444d5` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4717 | `239b95d444d5` | `9be46af46bad` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 25.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
