# Paquet integration/paquet-h-20261029-11 — composition (§12), sur main `f30f5380acea`

Base : `main` **`f30f5380acea1c717bafc844b07329a95824fb14`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261029-11`, cible `main`.
Porteur : **#4677**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #4299 | `90b8889300b9` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4299 | `90b8889300b9` | `5cbcc60d716d` | composition |

## Écartés

| lot | motif |
|---|---|
| #4574 | conflit, coupure suivante — fichiers : tests/test_l161f_plan.py |

Liste calculée : 2 lot(s) prêt(s), 7 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 23.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
