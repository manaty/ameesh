# Paquet integration/paquet-h-20261029-02 — composition (§12), sur main `4c0536139f81`

Base : `main` **`4c0536139f81486f2da512eb16f548cb0924aa97`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261029-02`, cible `main`.
Porteur : **#4440**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (14 unités, 14 lots)

| lot | source composée |
|---|---|
| #4274 | `57f8a442cc31560c5864cb0a24553ab7425e5161` |
| #4358 | `2cf0b5d67d49a2854479dfcba8c6cb786de80367` |
| #4430 | `abb6d5fe4bb790cf513a17d8e700c9e2d631520d` |
| #4166 | `52ad3c1fcc7c7ec35fbec220bc45d7d95c399483` |
| #4291 | `848dc9c468e9f22099215824d78d3baa188f89c2` |
| #4329 | `f912400a2f93edc32e4938c701dc25730c53372e` |
| #4351 | `08f7384be74286ae6ceff45b8cbffbd4ebb460ea` |
| #4355 | `404cb1530c506ceb0e5a2ca5ab43ecb730c9713f` |
| #4361 | `d0fa022fcbeae0e64c2438992d254f8a0cca37ee` |
| #4377 | `58db0d183bc57f472150c2084f7dc2d47c3a4962` |
| #4395 | `d80408939301aea8a0b36a5066b8c27545efdaa4` |
| #4401 | `4433329a9397838c4595e74d1a36d669aad0388b` |
| #4406 | `60d8f93ff28bb4bad2a1a27079cdf10906d4f84a` |
| #4339 | `a6e4bbe67c53dd41a1e8a9b08174eade2c4f37f4` |

## Composition (14 unités, 14 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4274 | `57f8a442cc31` | `cb3411a00145` | composition |
| 2 | #4358 | `2cf0b5d67d49` | `26c281180a4a` | composition |
| 3 | #4430 | `abb6d5fe4bb7` | `a44f0a8be538` | composition |
| 4 | #4166 | `52ad3c1fcc7c` | `23783def545c` | composition |
| 5 | #4291 | `848dc9c468e9` | `3241280eaf1a` | composition |
| 6 | #4329 | `f912400a2f93` | `b1c7a3f36d01` | composition |
| 7 | #4351 | `08f7384be742` | `8a9c320d2747` | composition |
| 8 | #4355 | `404cb1530c50` | `fbbba5e822c7` | composition |
| 9 | #4361 | `d0fa022fcbea` | `24bddbce75d3` | composition |
| 10 | #4377 | `58db0d183bc5` | `ea462e049e31` | composition |
| 11 | #4395 | `d80408939301` | `a55626dbe89e` | composition |
| 12 | #4401 | `4433329a9397` | `c64825b2ee3b` | composition |
| 13 | #4406 | `60d8f93ff28b` | `6b53bcf8ca92` | composition |
| 14 | #4339 | `a6e4bbe67c53` | `bba4b4623089` | composition |

## Écartés

| lot | motif |
|---|---|
| #4293 | conflit, coupure suivante — fichiers : tests/test_cluster.py |

Liste calculée : 15 lot(s) prêt(s), 8 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 21.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
