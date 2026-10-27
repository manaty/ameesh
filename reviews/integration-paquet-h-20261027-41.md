# Paquet integration/paquet-h-20261027-41 — composition (§12), sur main `5c03d85bc4b2`

Base : `main` **`5c03d85bc4b2736dbd4956402070eb5ef2f65330`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261027-41`, cible `main`.
Porteur : **#3997**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (16 unités, 16 lots)

| lot | source composée |
|---|---|
| #3974 | `3368b350eebcbee92eeb53c9823001065f243043` |
| #3976 | `fd719badc65e1a0cc1df80e1194156fcb215e084` |
| #3932 | `b136cbe43920cbd75d14113be66aa27254741c47` |
| #3954 | `8bc5c5d6ddd5bd9435a0abf9d9dd7fb788e30432` |
| #3963 | `9b9ca1968380bf1262a21061e35358300f80e064` |
| #3971 | `623363b0e1ead990511e5765d7ea9d4dcbdf4008` |
| #3985 | `1b528dd70576f738c89c7c2494d27d49858d5ee0` |
| #3991 | `dfcb73489053b6d9b5f1895472a19a6180a9e529` |
| #3996 | `45d7a4d1b8310553202693ed03b467add4d023da` |
| #4000 | `251bccb422140c207f47141e6a780f0c77fb3544` |
| #3949 | `c309b23bf113a220e2b4079febbad86f71c31707` |
| #3972 | `7b5d8e983e0e8889cc0630d0b65dfd09b02e21e5` |
| #3973 | `2f7b99c85f4a2a77d6907ac13dd72b7a7c79df21` |
| #3992 | `aa31fb6c174484cf936c5a8815f21dcf0753ce74` |
| #3999 | `5486e6636decfd1980877180a77e030780088859` |
| #4002 | `8430feeabd9db86b442cda3a3b3667ceef2fa479` |

## Composition (16 unités, 16 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3974 | `3368b350eebc` | `e082a898ac4a` | composition |
| 2 | #3976 | `fd719badc65e` | `5a32656aff22` | composition |
| 3 | #3932 | `b136cbe43920` | `46b73eb9b7f9` | composition |
| 4 | #3954 | `8bc5c5d6ddd5` | `03f0fc689a28` | composition |
| 5 | #3963 | `9b9ca1968380` | `d954d2ecd18e` | composition |
| 6 | #3971 | `623363b0e1ea` | `67876f5d0d9d` | composition |
| 7 | #3985 | `1b528dd70576` | `f1e26627a18e` | composition |
| 8 | #3991 | `dfcb73489053` | `53b2846657c4` | composition |
| 9 | #3996 | `45d7a4d1b831` | `d4ca4602886e` | composition |
| 10 | #4000 | `251bccb42214` | `db4ef21da506` | composition |
| 11 | #3949 | `c309b23bf113` | `ded7366e6840` | composition |
| 12 | #3972 | `7b5d8e983e0e` | `f8d7ae345be3` | composition |
| 13 | #3973 | `2f7b99c85f4a` | `433ce5554fc9` | composition |
| 14 | #3992 | `aa31fb6c1744` | `5e112a8585c9` | composition |
| 15 | #3999 | `5486e6636dec` | `fd4c62b40ece` | composition |
| 16 | #4002 | `8430feeabd9d` | `64d2857f821b` | composition |

## Écartés

| lot | motif |
|---|---|
| #3986 | retiré : ci-full rouge attribuée à son unité (#3801) — --exclude, recomposition sans lui |
| #3961 | retiré : ci-full rouge attribuée à son unité (#3801) — --exclude, recomposition sans lui |
| #3902 | conflit, coupure suivante — fichiers : src/ameesh/review_authors.py |

Liste calculée : 17 lot(s) prêt(s), 7 en attente, 2 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 19.3 s.
- Tests locaux : 17 séries, 36 modules, 1374 tests exécutés, 0 échec(s), 0 non concluant(s), 18 sauté(s) : `tests.test_1155_staging`, `tests.test_1770_prevalidation`, `tests.test_2340_auditeur_role`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_2644_prevalidation_calls`, `tests.test_3114_host_wide_drain`, `tests.test_339_orphan_settle`, `tests.test_3741_base_commune`, `tests.test_3752_gabarit`, `tests.test_3801_packet_red`, `tests.test_3949_detached_status`, `tests.test_3954_contrats_transverses`, `tests.test_3963_canon_sync_atomic`, `tests.test_3969_comptes_invalides`, `tests.test_3973_blocage_comptes`, `tests.test_3974_grep_cache`, `tests.test_3985_bound_session_provenance`, `tests.test_476_background_signals`, `tests.test_476_cleanup_limits`, `tests.test_503_agent_host`, `tests.test_789_slo`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l11_speed`, `tests.test_l157c_dates_mesures`, `tests.test_l160e2_land`, `tests.test_l161f_plan`, `tests.test_l165c_sessions_par_lot`, `tests.test_l31_orphans`, `tests.test_l50_group_orphan_work_items`, `tests.test_l72_panne_de_base`, `tests.test_l78_conversations`, `tests.test_l804_prise_executeur`, `tests.test_l93_device_state`, `tests.test_migrations`.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
