# Paquet integration/paquet-h-20261107-07 — composition (§12), sur main `474e66d6950b`

Base : `main` **`474e66d6950ba3b96c41a288d89954e03b7feddb`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-07`, cible `main`.
Porteur : **#6115**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #6092 | `1d1982de92a6` |
| #6104 | `be5c77133af8` |
| #6079 | `80e483cf2c90` |
| #6096 | `aeef32767aee` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6092 | `1d1982de92a6` | `64571a47030d` | composition |
| 2 | #6104 | `be5c77133af8` | `423e6b092e7a` | composition |
| 3 | #6079 | `80e483cf2c90` | `fce1a055d6ad` | composition |
| 4 | #6096 | `aeef32767aee` | `b21d120c4e36` | composition |

## Écartés

| lot | motif |
|---|---|
| #6091 | CI rouge : test_cluster SuperviseurRoleTest, les 7 colonnes de external_executors (migration 0554) ne sont ni accordées ni exclues du contrat des rôles superviseur — --exclude, recomposition sans lui |

Liste calculée : 4 lot(s) prêt(s), 3 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 42.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
