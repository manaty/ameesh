# Paquet integration/paquet-h-20261105-01 — composition (§12), sur main `328105969c88`

Base : `main` **`328105969c88337cd1171ce195c505ee98de0d5f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-01`, cible `main`.
Porteur : **#5807**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5792 | `8cc1d3b85e8a` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5792 | `8cc1d3b85e8a` | `a789f17199a1` | composition |

## Écartés

| lot | motif |
|---|---|
| #5778 | tests ciblés rouges (tests.test_2102_detacher_branche.DetachSweepTest.test_citation_explicite_du_lot_detache_reste_une_preuve, tests.test_2102_detacher_branche.DetachSweepTest.test_fusion_nommee_de_branche_detachee_ne_ferme_pas), situés au premier préfixe de l'empilement, retirés de la recomposition — préfixe base+u1…u1 (premier rouge à #5778) |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 41.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
