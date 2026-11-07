# Paquet integration/paquet-h-20261107-11 — composition (§12), sur main `f5752eaee681`

Base : `main` **`f5752eaee681550ee4b8a763e4084f0e0a2b15df`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-11`, cible `main`.
Porteur : **#6143**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6105 | `39b67e547842` |
| #6134 | `8b2f1f1e2d42` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6105 | `39b67e547842` | `f20dd85b38ce` | composition |
| 2 | #6134 | `8b2f1f1e2d42` | `fe4483927b31` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 53.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
