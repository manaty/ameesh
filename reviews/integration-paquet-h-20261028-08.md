# Paquet integration/paquet-h-20261028-08 — composition (§12), sur main `8f7b2fb7b861`

Base : `main` **`8f7b2fb7b86152830bc6254c2f7ca8e60edf2ce3`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261028-08`, cible `main`.
Porteur : **#4374**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (18 unités, 18 lots)

| lot | source composée |
|---|---|
| #4325 | `1386401cae96d6122c37f11674c4dd6899004124` |
| #4375 | `532d161805fd2034b8d5ad8f2ced005dcc08409c` |
| #4186 | `85a620dc3d690c51e723b0dd610ceb86813e5391` |
| #4237 | `cd1e217f7348497c6eec4da42612d4ed97b6cf18` |
| #4248 | `f7820d4af044d1dc9d80d0d5cdc102a8aeae7b38` |
| #4250 | `06a21d73f5ec78717bf98d318490dd41bb097bc8` |
| #4295 | `476276eb36950357290664675b707ff9e374d2e8` |
| #4298 | `893176dd5a96f172ba8ed6ee7b9835ec32337317` |
| #4323 | `783f17a2e997b81c393e34ce1f1ec8fef1cf84f2` |
| #4331 | `c215723c7940752c55d11fdf01d51903235a4c94` |
| #4342 | `f9a267d0b0acf933424f10901b019bb9809293f9` |
| #4343 | `3a330c13b1d263f86e95ae10abc31b8016fcd19c` |
| #4349 | `684b5fcbd756221db6610dbae7adc048bf1c5e5d` |
| #4178 | `8bc2bf7663a3aaf0e0ee57e48b7db6a03ac8166c` |
| #4255 | `fcd5af612dbcbcb89a4582808409acdcc05a41ff` |
| #4301 | `588f2ce873554861c314ff30c79634a3303c510e` |
| #4307 | `642edb677028b52b96c2ba06237fe98fe190c578` |
| #4322 | `b11f5f8aaf7140db6f6a573795109d7aa2decf59` |

## Composition (18 unités, 18 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4325 | `1386401cae96` | `5cdbb195c17f` | composition |
| 2 | #4375 | `532d161805fd` | `31f7b517dac3` | composition |
| 3 | #4186 | `85a620dc3d69` | `574644a2c43b` | composition |
| 4 | #4237 | `cd1e217f7348` | `23a16ec294ae` | composition |
| 5 | #4248 | `f7820d4af044` | `b6a698b86b65` | composition |
| 6 | #4250 | `06a21d73f5ec` | `c677d8f5bf2b` | composition |
| 7 | #4295 | `476276eb3695` | `ca17c2727728` | composition |
| 8 | #4298 | `893176dd5a96` | `48bfdae2c6c7` | composition |
| 9 | #4323 | `783f17a2e997` | `d37abe2946c8` | composition |
| 10 | #4331 | `c215723c7940` | `ef90bd69cfb7` | composition |
| 11 | #4342 | `f9a267d0b0ac` | `8a4527991d52` | composition |
| 12 | #4343 | `3a330c13b1d2` | `bd2ebc602d84` | composition |
| 13 | #4349 | `684b5fcbd756` | `ae39477723c4` | composition |
| 14 | #4178 | `8bc2bf7663a3` | `19d35823d688` | composition |
| 15 | #4255 | `fcd5af612dbc` | `1bbd04058219` | composition |
| 16 | #4301 | `588f2ce87355` | `d081c9dabf56` | composition |
| 17 | #4307 | `642edb677028` | `b5ecbb30a22e` | composition |
| 18 | #4322 | `b11f5f8aaf71` | `9759efccbf8d` | composition |

## Écartés

| lot | motif |
|---|---|
| #4174 | test négatif, rouge seul sur sa tête 10d891f : test_811_lot_workdirs.PickupTest (run 40762892421) ; tête corrigée 905c586 pour le paquet suivant — --exclude, recomposition sans lui |
| #4292 | test négatif, rouge seul sur sa tête 0902e4c : test_2179 aide racine non alphabétique (run 40762892421) — --exclude, recomposition sans lui |
| #4293 | test négatif, rouge seul sur sa tête 250d0fa : test_2179 aide racine (run 40762892421) ; tête corrigée 51a4380 pour le paquet suivant — --exclude, recomposition sans lui |
| #4358 | test négatif, rouge seul sur sa tête 60cbcc9 : test_3114_host_wide_drain (run 40762892421) — --exclude, recomposition sans lui |
| #4354 | test négatif, rouge seul sur sa tête 363db20 : test_doc_links, BASCULE.md:132 ancre EXPLOITATION.md#dossier-de-travail-des-agents-l31-l35 manquante (run 40767227215) — --exclude, recomposition sans lui |
| #4291 | test négatif, rouge seul sur sa tête 46938b3 : test_2179 aide racine, entrée veille scan hors ordre (run 40767227215) — --exclude, recomposition sans lui |
| #4350 | conflit, coupure suivante — fichiers : docs/design/generic-core-boundary.md, src/ameesh/work_lifecycle.py |
| #4361 | conflit, coupure suivante — fichiers : docs/EXPLOITATION.md, scripts/paquet-horaire.py |

Liste calculée : 20 lot(s) prêt(s), 11 en attente, 6 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 22.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
