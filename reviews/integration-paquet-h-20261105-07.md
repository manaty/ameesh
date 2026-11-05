# Paquet integration/paquet-h-20261105-07 — composition (§12), sur main `04d31ddb4f1b`

Base : `main` **`04d31ddb4f1ba922c26d8b087d52000fe54bcab9`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-07`, cible `main`.
Porteur : **#5878**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5864 | `f39a5179060d` |
| #5826 | `9815ec824ca5` |
| #5867 | `8e3c9461c2a3` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5864 | `f39a5179060d` | `97a1e5750b72` | composition |
| 2 | #5826 | `9815ec824ca5` | `315df236f719` | composition |
| 3 | #5867 | `8e3c9461c2a3` | `4355b5c5a21f` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 5 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 43.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
