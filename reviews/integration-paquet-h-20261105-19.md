# Paquet integration/paquet-h-20261105-19 — composition (§12), sur main `e789c1c2582f`

Base : `main` **`e789c1c2582fce9c82421ebda0cd1004fde13c0c`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-19`, cible `main`.
Porteur : **#5923**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5914 | `dd851dc2cfc5` |
| #5921 | `07d650ff29a5` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5914 | `dd851dc2cfc5` | `35ef1c4d5fee` | composition |
| 2 | #5921 | `07d650ff29a5` | `7ed2d516accd` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 40.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
