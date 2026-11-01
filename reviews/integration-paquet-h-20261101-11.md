# Paquet integration/paquet-h-20261101-11 — composition (§12), sur main `44483743851c`

Base : `main` **`44483743851cb7f35607cb7abf2dbc88d8b2ea74`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261101-11`, cible `main`.
Porteur : **#5303**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #5266 | `a5e4480ef2cf` |
| #5287 | `f7d1f518e559` |
| #5250 | `3b409d8aa1da` |
| #5293 | `f7c893b8ae78` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5266 | `a5e4480ef2cf` | `f7f2b636599f` | composition |
| 2 | #5287 | `f7d1f518e559` | `204d8c4eda3b` | composition |
| 3 | #5250 | `3b409d8aa1da` | `5bba004bcad5` | composition |
| 4 | #5293 | `f7c893b8ae78` | `d5e3f9bfbff9` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 24.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
