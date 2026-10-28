# Paquet integration/paquet-h-20261028-02 — composition (§12), sur main `904939b62e75`

Base : `main` **`904939b62e75d5625859d87192dc6082150b0c04`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261028-02`, cible `main`.
Porteur : **#4125**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #4086 | `13b2146077b33179cfa05e8071ee56cfef230e4f` |
| #4114 | `265071b445b98c6ba67bb3cabd2e5a5fd2d1a9cd` |
| #4115 | `9d59b03db6c12edc2f237c52ecdce57e87562cad` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4086 | `13b2146077b3` | `46713104021a` | composition |
| 2 | #4114 | `265071b445b9` | `123880484e34` | composition |
| 3 | #4115 | `9d59b03db6c1` | `e1e2745fed5d` | composition |

## Écartés

| lot | motif |
|---|---|
| #4111 | tests locaux rouges (tests.test_789_slo_alerts, tests.test_3512_slo_merged, tests.test_3632_merge_observed_vehicle), retiré de la recomposition |

Liste calculée : 4 lot(s) prêt(s), 6 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 14.3 s.
- Tests locaux : 4 séries, 91 modules, 1969 tests exécutés, 0 échec(s), 0 run(s) rouge(s) sans effet, 0 non concluant(s), 24 sauté(s) : `tests.test_1021_tenue_attribution`, `tests.test_1266_unreadable_turn_window`, `tests.test_1272_rejeu_seul_affiche`, `tests.test_1349_agent_peek`, `tests.test_1425_stop_sweep_suites`, `tests.test_1465_provenance`, `tests.test_1466_relecture_config`, `tests.test_1467_train`, `tests.test_1467_train_events`, `tests.test_1467_train_service`, `tests.test_1467_train_transports`, `tests.test_1580_marge_forfait`, `tests.test_1580_widget_forfait`, `tests.test_1651_ci_parts`, `tests.test_1656_auto_merge`, `tests.test_1672_placement_releve_perime`, `tests.test_1681_incident`, `tests.test_1682_auto_merge_constat`, `tests.test_1718_land_eject_unreadable`, `tests.test_2167_host_housekeeping`, `tests.test_2212_fire_fresh_radar`, `tests.test_2286_queue_required_checks`, `tests.test_2339_autotake_migration`, `tests.test_2340_auditeur_role`, `tests.test_2345_land_retry_bound`, `tests.test_2389_fire_no_land_once`, `tests.test_2428_radar_clone_lock`, `tests.test_2432_land_head_clonebusy`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_2526_orphan_dependance`, `tests.test_2582_alerts_console_timeout`, `tests.test_2623_orphan_levee_dependance`, `tests.test_2689_reveil_statut`, `tests.test_2743_housekeeping_clones`, `tests.test_2839_codex_account_gauges`, `tests.test_2877_stop_sweep_without_lock`, `tests.test_2933_housekeeping_alert_identity`, `tests.test_2934_budget_pause_notes`, `tests.test_3007_notify_fallback`, `tests.test_3035_host_reconcile`, `tests.test_3080_fire_queue_landing`, `tests.test_313_comptes_deux_meshes`, `tests.test_3273_idle_prompt_pilot`, `tests.test_3307_basket_3300`, `tests.test_3327_role_timers`, `tests.test_3379_argparse_help`, `tests.test_3383_alerts_role_timer`, `tests.test_339_orphan_settle`, `tests.test_3402_alerts_advisory`, `tests.test_3408_watch_foreign_pin`, `tests.test_3472_handoff_runner`, `tests.test_3536_notify_nexlink`, `tests.test_3568_alerts_delegate`, `tests.test_3575_alerts_basket`, `tests.test_3619_host_crypto`, `tests.test_3752_gabarit`, `tests.test_4086_restart_reservation`, `tests.test_472_zombie_turn_close`, `tests.test_501_native_host_gate`, `tests.test_508_runner_liveness`, `tests.test_653_db_connections`, `tests.test_706_rotation_latency`, `tests.test_753_stop_returns_lots`, `tests.test_bin_launchers`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l120_harness_versions`, `tests.test_l160e2_land`, `tests.test_l160g_nexlink`, `tests.test_l161f_plan`, `tests.test_l165_election`, `tests.test_l165_ep2_election_score`, `tests.test_l165_ep3_election_standby`, `tests.test_l232b_bootstrap_installer`, `tests.test_l234_auditor`, `tests.test_l242b_critical_accounts`, `tests.test_l242c_critical_roles`, `tests.test_l31_orphans`, `tests.test_l490_deepseek_cgroup`, `tests.test_l498_turn_cgroup`, `tests.test_l50_group_orphan_work_items`, `tests.test_l53_session_backup`, `tests.test_l72_panne_de_base`, `tests.test_l77_resident_queue`, `tests.test_l780_outdated_runner`, `tests.test_l795_preemption_evenement`, `tests.test_l804_prise_executeur`, `tests.test_l93_device_state`, `tests.test_migrations`, `tests.test_path_b_assembly`.

Modules voisins joués (support importé, carte d'impact) : carte d'impact `6d67072c3787` (lue, antérieure à la base) ; budget de carte 180 s par unité, 232 s pris, 114 module(s) au-delà du budget non joué(s).

| unité | module | raison | fichiers de l'unité |
|---|---|---|---|
| unité 4086 | `tests.test_3273_idle_prompt_pilot` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_2212_fire_fresh_radar` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_2389_fire_no_land_once` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_706_rotation_latency` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l490_deepseek_cgroup` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_1266_unreadable_turn_window` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_313_comptes_deux_meshes` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_path_b_assembly` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_1272_rejeu_seul_affiche` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_1466_relecture_config` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l234_auditor` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_1681_incident` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_1467_train_service` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_501_native_host_gate` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_3307_basket_3300` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l53_session_backup` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l77_resident_queue` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_1672_placement_releve_perime` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_508_runner_liveness` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_653_db_connections` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l498_turn_cgroup` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l242c_critical_roles` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_2877_stop_sweep_without_lock` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l780_outdated_runner` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_1467_train_events` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l93_device_state` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_3035_host_reconcile` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l795_preemption_evenement` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l232b_bootstrap_installer` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_3408_watch_foreign_pin` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_2933_housekeeping_alert_identity` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_3575_alerts_basket` | carte | `src/ameesh/election.py`, `src/ameesh/role_standings_pg.py` |
| unité 4086 | `tests.test_2339_autotake_migration` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_2839_codex_account_gauges` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_3007_notify_fallback` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_3472_handoff_runner` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_1580_marge_forfait` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_472_zombie_turn_close` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l31_orphans` | carte | `src/ameesh/election.py`, `src/ameesh/role_handover_pg.py`, `src/ameesh/runner.py` |
| unité 4086 | `tests.test_753_stop_returns_lots` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_bin_launchers` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_1580_widget_forfait` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_2743_housekeeping_clones` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_2623_orphan_levee_dependance` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l242b_critical_accounts` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_339_orphan_settle` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_1349_agent_peek` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l165_election` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l120_harness_versions` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_l804_prise_executeur` | carte | `src/ameesh/runner.py` |
| unité 4086 | `tests.test_1465_provenance` | carte | `src/ameesh/runner.py` |
| unité 4114 | `tests.test_l165_ep3_election_standby` | carte | `src/ameesh/election.py`, `src/ameesh/role_timers.py` |
| unité 4114 | `tests.test_l165_ep2_election_score` | carte | `src/ameesh/election.py`, `src/ameesh/role_timers.py` |
| unité 4114 | `tests.test_3568_alerts_delegate` | carte | `src/ameesh/election.py`, `src/ameesh/role_timers.py` |
| unité 4114 | `tests.test_3402_alerts_advisory` | carte | `src/ameesh/role_timers.py` |
| unité 4114 | `tests.test_3327_role_timers` | carte | `src/ameesh/role_timers.py` |
| unité 4114 | `tests.test_3383_alerts_role_timer` | carte | `src/ameesh/role_timers.py` |
| unité 4114 | `tests.test_3619_host_crypto` | carte | `src/ameesh/role_timers.py` |
| unité 4114 | `tests.test_3536_notify_nexlink` | carte | `src/ameesh/role_timers.py` |
| unité 4114 | `tests.test_3379_argparse_help` | carte | `src/ameesh/role_timers.py` |
| unité 4115 | `tests.test_1467_train` | support | `tests/test_l160e2_land.py` |
| unité 4115 | `tests.test_1467_train_transports` | support | `tests/test_l160e2_land.py` |
| unité 4115 | `tests.test_1651_ci_parts` | support | `tests/test_l160e2_land.py` |
| unité 4115 | `tests.test_1656_auto_merge` | support | `tests/test_l160e2_land.py` |
| unité 4115 | `tests.test_1682_auto_merge_constat` | support | `tests/test_l160e2_land.py` |
| unité 4115 | `tests.test_1718_land_eject_unreadable` | support | `tests/test_l160e2_land.py` |
| unité 4115 | `tests.test_2286_queue_required_checks` | support | `tests/test_l160e2_land.py` |
| unité 4115 | `tests.test_2345_land_retry_bound` | support | `tests/test_l160e2_land.py` |
| unité 4115 | `tests.test_2428_radar_clone_lock` | support | `tests/test_l160e2_land.py` |
| unité 4115 | `tests.test_2432_land_head_clonebusy` | support | `tests/test_l160e2_land.py` |
| unité 4115 | `tests.test_3080_fire_queue_landing` | support | `tests/test_l160e2_land.py` |
| unité 4115 | `tests.test_l160g_nexlink` | support | `tests/test_l160e2_land.py` |

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
