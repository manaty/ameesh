# Paquet integration/paquet-h-20261027-40 — composition (§12), sur main `7b11c78a4096`

Base : `main` **`7b11c78a4096d67728e471c31af700b25430e250`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261027-40`, cible `main`.
Porteur : **#3988**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (9 unités, 9 lots)

| lot | source composée |
|---|---|
| #3956 | `924878ca468958d9a45c0dedb99cabc1ec914117` |
| #3965 | `1b490544a38c4833f005a454641f703dea7a9c1e` |
| #3970 | `753dd37d26d731632ac454faf6a70818efa85789` |
| #3913 | `814381b1c389c8b3ac6cfe2a711d385085426e99` |
| #3948 | `518639d514fbcb4d926cfcc774bc8669e4278394` |
| #3968 | `9d2e3f9f743e332dedd54f87c5278099e7b3bf73` |
| #3939 | `38cab4a66251439cc4412c480d806936aa411adb` |
| #3959 | `470c10a4cc5f287361fb0e4bfaf1350d21283005` |
| #3964 | `0fdcc081eaf56ddac3b3b7fdfbca42a86e7dc6dd` |

## Composition (9 unités, 9 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3956 | `924878ca4689` | `5576c8a3767e` | composition |
| 2 | #3965 | `1b490544a38c` | `064ff6fd6618` | composition |
| 3 | #3970 | `753dd37d26d7` | `8dddce9a4169` | composition |
| 4 | #3913 | `814381b1c389` | `a497450fe0c9` | composition |
| 5 | #3948 | `518639d514fb` | `32ca15b8460a` | composition |
| 6 | #3968 | `9d2e3f9f743e` | `d4082c047164` | composition |
| 7 | #3939 | `38cab4a66251` | `0437b0a3938c` | composition |
| 8 | #3959 | `470c10a4cc5f` | `d5c2a95ddc0c` | composition |
| 9 | #3964 | `0fdcc081eaf5` | `5a7f56cb1ae1` | composition |

## Écartés

| lot | motif |
|---|---|
| #3976 | conflit, coupure suivante — fichiers : deploy/mise-a-jour/staging.sh |
| #3963 | conflit, coupure suivante — fichiers : docs/EXECUTEUR-MEDIEE.md |

Liste calculée : 11 lot(s) prêt(s), 10 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 28.7 s.
- Tests locaux : 9 séries, 28 modules, 995 tests exécutés, 0 échec(s), 0 non concluant(s), 10 sauté(s) : `tests.test_1155_staging`, `tests.test_1323_actif`, `tests.test_2340_auditeur_role`, `tests.test_2447_review_designation_activation`, `tests.test_2448_release_cutoff`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_2739_review_correction`, `tests.test_2888_reviewer_fix_independence`, `tests.test_2924_history_rewrite_provenance`, `tests.test_3007_notify_fallback`, `tests.test_339_orphan_settle`, `tests.test_3752_gabarit`, `tests.test_3792_menage_venvs`, `tests.test_3904_remote_tip_batch`, `tests.test_3913_notify_lease`, `tests.test_3955_canon_commit_monotone`, `tests.test_3968_gate_cause`, `tests.test_532_pgbouncer_pool`, `tests.test_653_db_connections`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l161f_plan`, `tests.test_l230_build_artifacts`, `tests.test_l31_orphans`, `tests.test_l50_group_orphan_work_items`, `tests.test_l72_panne_de_base`, `tests.test_migrations`.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
