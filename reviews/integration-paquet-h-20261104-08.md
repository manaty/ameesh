# Paquet integration/paquet-h-20261104-08 — composition (§12), sur main `2d65a86ac1e4`

Base : `main` **`2d65a86ac1e4ef35ee35f5602eab88e18a8f5232`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-08`, cible `main`.
Porteur : **#5731**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5703 | `c8feab3cf786` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5703 | `c8feab3cf786` | `bc0feaeddb6a` | composition |

## Écartés

| lot | motif |
|---|---|
| #5729 | tests ciblés rouges (tests.test_l68_macos_ci.CiMacosCoexistenceTest.test_ci_macos_silences_every_tests_job, unittest.loader._FailedTest.test_5729_qualified_tree), situés au premier préfixe de l'empilement, retirés de la recomposition — préfixe base+u1…u1 (premier rouge à #5729) |

Liste calculée : 2 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 36.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
