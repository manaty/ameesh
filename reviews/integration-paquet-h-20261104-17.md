# Paquet integration/paquet-h-20261104-17 — composition (§12), sur main `5b253678ce0b`

Base : `main` **`5b253678ce0b3adf5964e1323fc51b78808d8649`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-17`, cible `main`.
Porteur : **#5769**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5753 | `df783d962d52` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5753 | `df783d962d52` | `fba9bacde47c` | composition |

## Écartés

| lot | motif |
|---|---|
| #5755 | conflit, coupure suivante — fichiers : src/ameesh/ci.py |

Liste calculée : 2 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 36.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
