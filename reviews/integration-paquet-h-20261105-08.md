# Paquet integration/paquet-h-20261105-08 — composition (§12), sur main `04d31ddb4f1b`

Base : `main` **`04d31ddb4f1ba922c26d8b087d52000fe54bcab9`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-08`, cible `main`.
Porteur : **#5879**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5864 | `f39a5179060d` |
| #5867 | `8e3c9461c2a3` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5864 | `f39a5179060d` | `580797fde1aa` | composition |
| 2 | #5867 | `8e3c9461c2a3` | `4f1d92ac7fc0` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 45.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
