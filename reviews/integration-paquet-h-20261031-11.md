# Paquet integration/paquet-h-20261031-11 — composition (§12), sur main `093336b231c6`

Base : `main` **`093336b231c63ff2f957c25a6f9f78d8c97ab449`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-11`, cible `main`.
Porteur : **#5098**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5084 | `82c102325867` |
| #5062 | `516a43eca74c` |
| #5064 | `58b15b909d79` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5084 | `82c102325867` | `3dbb83e57058` | composition |
| 2 | #5062 | `516a43eca74c` | `b6d9ae1c2dba` | composition |
| 3 | #5064 | `58b15b909d79` | `2022e684acd3` | composition |

## Écartés

| lot | motif |
|---|---|
| #5094 | tests ciblés rouges (tests.test_3311_preuve_venv.ControlerServeurTest.test_controler_vert_si_le_runner_serveur_vit_sur_le_venv_courant), attribués à son unité (#3801), retirés de la recomposition — test tests/test_3311_preuve_venv.py |

Liste calculée : 4 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 22.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
