# Paquet integration/paquet-h-20261103-16 — composition (§12), sur main `7ba8c77ebba3`

Base : `main` **`7ba8c77ebba385dbe61c5986f8d5150264261efd`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-16`, cible `main`.
Porteur : **#5660**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #5635 | `369c726c4fa3` |
| #5649 | `37e0e7ac6491` |
| #5652 | `4ca28efe2ddd` |
| #5656 | `5e582c0e8510` |
| #5598 | `e039f9a14b8f` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5635 | `369c726c4fa3` | `bb35bc6df1c6` | composition |
| 2 | #5649 | `37e0e7ac6491` | `6fc9c01f0ed0` | composition |
| 3 | #5652 | `4ca28efe2ddd` | `cb992aa86941` | composition |
| 4 | #5656 | `5e582c0e8510` | `47bce5886507` | composition |
| 5 | #5598 | `e039f9a14b8f` | `a7c9ef638227` | composition |

## Écartés

| lot | motif |
|---|---|
| #5655 | tests ciblés rouges (tests.test_l161c_failures_realtime.WatcherTest.test_rouge_sans_flux_lit_les_not_ok_tap), situés au premier préfixe de l'empilement, retirés de la recomposition — préfixe base+u1…u4 (premier rouge à #5655) |

Liste calculée : 6 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 40.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
