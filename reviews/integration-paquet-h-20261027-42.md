# Paquet integration/paquet-h-20261027-42 — composition (§12), sur main `40710da4aa16`

Base : `main` **`40710da4aa169f8e99c812c28b493149a2a4654d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261027-42`, cible `main`.
Porteur : **#4018**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (10 unités, 10 lots)

| lot | source composée |
|---|---|
| #4016 | `d0aaa9ad8365b887142784226290bc1931debeba` |
| #3889 | `a15d51ce13860b721de9fc1bc902f6920b0093fe` |
| #3902 | `88d88ab15562e507ac99c08e2dd7fd423d487e88` |
| #4006 | `b241abb43ba98cbb0792ecf5fafe93b5117e926e` |
| #4008 | `d7735eb94ba6ccbaa5819ed06bb7acbffd313ce2` |
| #4011 | `d4d20b77822114f714e1701096ae2e817bca5a49` |
| #4020 | `58d2ff06ac407bdc5472c4e8c1bb621236b566c9` |
| #4023 | `51bd6475cbc31d37558fbf40ae95e2205e767cca` |
| #3960 | `756e3801932f2c37120db7a028675099d0244cee` |
| #4029 | `fa2c7f29922ecb985eb74590d55b3310266ce7bf` |

## Composition (10 unités, 10 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4016 | `d0aaa9ad8365` | `9e26563fbf2a` | composition |
| 2 | #3889 | `a15d51ce1386` | `b194497eef9b` | composition |
| 3 | #3902 | `88d88ab15562` | `bf15f9f6f782` | composition |
| 4 | #4006 | `b241abb43ba9` | `168d5eac9386` | composition |
| 5 | #4008 | `d7735eb94ba6` | `cea770ebc5c0` | composition |
| 6 | #4011 | `d4d20b778221` | `ad9451956c72` | composition |
| 7 | #4020 | `58d2ff06ac40` | `074691a6275b` | composition |
| 8 | #4023 | `51bd6475cbc3` | `b357d3702438` | composition |
| 9 | #3960 | `756e3801932f` | `fc3a96b1ccda` | composition |
| 10 | #4029 | `fa2c7f29922e` | `f49cbacf6552` | composition |

## Écartés

| lot | motif |
|---|---|
| #3986 | retiré : ci-full rouge attribuée à son unité (#3801) — --exclude, recomposition sans lui |
| #3961 | retiré : ci-full rouge attribuée à son unité (#3801) — --exclude, recomposition sans lui |
| #3981 | retiré : ci-full rouge attribuée à son unité (#3801) — --exclude, recomposition sans lui |
| #3993 | conflit, coupure suivante — fichiers : tests/test_l160e2_land.py |

Liste calculée : 11 lot(s) prêt(s), 3 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 18.7 s.
- Tests locaux : 11 séries, 30 modules, 1141 tests exécutés, 0 échec(s), 0 non concluant(s), 14 sauté(s) : `tests.test_1770_prevalidation`, `tests.test_2340_auditeur_role`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_2644_prevalidation_calls`, `tests.test_2934_budget_pause_notes`, `tests.test_339_orphan_settle`, `tests.test_3752_gabarit`, `tests.test_3781_target_landing_provenance`, `tests.test_3902_target_content_provenance`, `tests.test_3913_notify_lease`, `tests.test_4023_note_tiret_stdin`, `tests.test_approve`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l159b_offload`, `tests.test_l160e2_land`, `tests.test_l161f_plan`, `tests.test_l31_orphans`, `tests.test_l3889_approve_js`, `tests.test_l3889_progress_outbox_js`, `tests.test_l3889_sans_erreur_serveur`, `tests.test_l50_group_orphan_work_items`, `tests.test_l72_panne_de_base`, `tests.test_migrations`, `tests.test_process_approvals`, `tests.test_process_approvals_704`, `tests.test_process_registry`, `tests.test_process_verifiers`, `tests.test_progress_serve`.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
