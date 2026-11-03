# Paquet integration/paquet-h-20261103-12 — composition (§12), sur main `619ff3d4e6c5`

Base : `main` **`619ff3d4e6c59c20cc98cdae108ff8809c401da8`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-12`, cible `main`.
Porteur : **#5605**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5595 | `6252bfedb0cf` |
| #5588 | `e59e64c96fa5` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5595 | `6252bfedb0cf` | `a78e0c9d32a3` | composition |
| 2 | #5588 | `e59e64c96fa5` | `e9de00597f73` | composition |

## Écartés

| lot | motif |
|---|---|
| #5585 | tests ciblés rouges (tests.test_5585_journal_plans.JournalPlanDbTest.test_plans_use_the_item_index_and_plan_under_5ms), attribués à son unité (#3801), retirés de la recomposition — test tests/test_5585_journal_plans.py |

Liste calculée : 3 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 41.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
