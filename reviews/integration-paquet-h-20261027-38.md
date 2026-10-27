# Paquet integration/paquet-h-20261027-38 — composition (§12), sur main `e865047a4b12`

Base : `main` **`e865047a4b121e525e2ecdd2d4837bf8e0fb1934`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261027-38`, cible `main`.
Porteur : **#3966**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (13 unités, 13 lots)

| lot | source composée |
|---|---|
| #3885 | `ffb24d20a41531bd0fd4f83be0cfcbdc4ad2cf82` |
| #3916 | `4cee31716abfad69ea301a01802007acdfa63748` |
| #3921 | `dec852b4896d35cacedfb0b9df39e4651fb8fc45` |
| #3922 | `226d5f65a260f938ec8e0f9a33d334b70d2e6d4a` |
| #3935 | `7d5e4c6dedc710a65e8157433194ec073cb0871f` |
| #3938 | `9af411a7e389e4bbc49bbcd9a293f3e0ae702e75` |
| #3941 | `02ecb445396aa913efd4bbda98ed7b7180d1f0b5` |
| #3945 | `b6a4b5a4b9c32c332ccef518587e59ed68aab651` |
| #3946 | `83c1e84538e50b5b93b8dbf9d9e19ec887486874` |
| #3947 | `904e6c1319c8f95dafa3d41512def11f79a4fabf` |
| #3955 | `58c38ff9fd950aa6672d7599d2dfd4a24da030ba` |
| #3958 | `fb21b541acf9a9c3a214b118127ed13f706d5ee0` |
| #3942 | `527ad17101d43dcb63102aa7d1d23cbe03cbf8a0` |

## Composition (13 unités, 13 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3885 | `ffb24d20a415` | `87813ebc767e` | composition |
| 2 | #3916 | `4cee31716abf` | `14c00e1e4ef1` | composition |
| 3 | #3921 | `dec852b4896d` | `3b1d47f14937` | composition |
| 4 | #3922 | `226d5f65a260` | `9038f77a1e8d` | composition |
| 5 | #3935 | `7d5e4c6dedc7` | `1274014e369c` | composition |
| 6 | #3938 | `9af411a7e389` | `aef03ad895de` | composition |
| 7 | #3941 | `02ecb445396a` | `dbdebbec0a24` | composition |
| 8 | #3945 | `b6a4b5a4b9c3` | `2b9ca32650a5` | composition |
| 9 | #3946 | `83c1e84538e5` | `983894c5354b` | composition |
| 10 | #3947 | `904e6c1319c8` | `831a714cac32` | composition |
| 11 | #3955 | `58c38ff9fd95` | `e8a1b21ed4a4` | composition |
| 12 | #3958 | `fb21b541acf9` | `6f9ab9538f68` | composition |
| 13 | #3942 | `527ad17101d4` | `88cfb45bc57e` | composition |

## Écartés

| lot | motif |
|---|---|
| #3913 | conflit, coupure suivante — fichiers : docs/EXECUTEUR-MEDIEE.md, src/ameesh/mediated_executor/contract.json |

Liste calculée : 14 lot(s) prêt(s), 8 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 26.3 s.
- Tests locaux : 14 séries, 27 modules, 653 tests exécutés, 0 échec(s), 0 non concluant(s), 24 sauté(s) : `tests.test_2340_auditeur_role`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_2702_class_at_merge_paquet`, `tests.test_3397_alerts_lenteur`, `tests.test_339_orphan_settle`, `tests.test_3752_gabarit`, `tests.test_3754_parallel_parts`, `tests.test_3807_paquet_auto`, `tests.test_3845_paquet_horaire`, `tests.test_3885_fingerprints`, `tests.test_3921_host_presence`, `tests.test_3922_gauge_seeks`, `tests.test_3923_listing_soldes`, `tests.test_3931_paquet_rouge_cible`, `tests.test_3945_host_cap`, `tests.test_3955_canon_commit_monotone`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l112_host_gate`, `tests.test_l131_suites_lourdes`, `tests.test_l161f_plan`, `tests.test_l31_orphans`, `tests.test_l50_group_orphan_work_items`, `tests.test_l72_panne_de_base`, `tests.test_migrations`, `tests.test_work`.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
