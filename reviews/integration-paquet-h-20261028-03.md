# Paquet integration/paquet-h-20261028-03 — composition (§12), sur main `247e2e8c2198`

Base : `main` **`247e2e8c21984936ff66eecab25ff7ec5cce12f8`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261028-03`, cible `main`.
Porteur : **#4141**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (7 unités, 7 lots)

| lot | source composée |
|---|---|
| #4126 | `4a4b4afd72766c022d2b538304f6278d81b44fa2` |
| #4097 | `6d4af0780a6f502c13e45eebfe5c32a891cf369f` |
| #4109 | `2101754d0908e3e24acd5da53a9feffce4973bbe` |
| #4111 | `a85c10b1970c046b73a93fc1081ae7699eb75e66` |
| #4112 | `c0ee3acab1e8bae2d3d71d6ff62786e1f90be0e5` |
| #4136 | `e54bed8d4c3b095c24f302eb9f202eae1e0583c4` |
| #4117 | `4f62a16c7b3e032854af6c63d0cdeb3001f04efe` |

## Composition (7 unités, 7 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4126 | `4a4b4afd7276` | `3d101679cb82` | composition |
| 2 | #4097 | `6d4af0780a6f` | `509152c4920c` | composition |
| 3 | #4109 | `2101754d0908` | `f685b06712da` | composition |
| 4 | #4111 | `a85c10b1970c` | `cab738412526` | composition |
| 5 | #4112 | `c0ee3acab1e8` | `0ee744a0bb4e` | composition |
| 6 | #4136 | `e54bed8d4c3b` | `075df5a8b7ae` | composition |
| 7 | #4117 | `4f62a16c7b3e` | `06ec11a1e231` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 7 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 20.9 s.
- Tests locaux : 8 séries, 215 modules, 4859 tests exécutés, 0 échec(s), 0 run(s) rouge(s) sans effet, 1 non concluant(s), 10 sauté(s) : `tests.test_1002_relance_perimee`, `tests.test_1021_tenue_attribution`, `tests.test_1022_creneau_landed`, `tests.test_1053_famille_au_gel`, `tests.test_1075_pace_multiplier`, `tests.test_1127_config_explicite`, `tests.test_1155_staging`, `tests.test_1227_suite_1176`, `tests.test_1266_unreadable_turn_window`, `tests.test_1272_rejeu_seul_affiche`, `tests.test_1280_work_close_done`, `tests.test_1286_declared_merge_proof`, `tests.test_1318_verdict_perime`, `tests.test_1323_actif`, `tests.test_1349_agent_peek`, `tests.test_1391_backlog_route`, `tests.test_1400_declaration_fusion`, `tests.test_1425_stop_sweep_suites`, `tests.test_1466_relecture_config`, `tests.test_1467_train_cli`, `tests.test_1467_train_events`, `tests.test_1467_train_fire`, `tests.test_1467_train_runtime`, `tests.test_1467_train_service`, `tests.test_1467_train_storage`, `tests.test_1561_fraicheur_releves`, `tests.test_1580_marge_forfait`, `tests.test_1580_widget_forfait`, `tests.test_1647_liste_lots_journal`, `tests.test_1672_placement_releve_perime`, `tests.test_1680_packet_reservation`, `tests.test_1681_incident`, `tests.test_1703_writer_identity`, `tests.test_1770_prevalidation_cli`, `tests.test_1770_prevalidation_store`, `tests.test_1901_gate_unreadable`, `tests.test_2022_fleet`, `tests.test_2022_host_zombies`, `tests.test_2096_runner_memoire`, `tests.test_2102_detacher_branche`, `tests.test_2130_d_projection`, `tests.test_2167_host_housekeeping`, `tests.test_2211_roadmap_followups`, `tests.test_2212_fire_fresh_radar`, `tests.test_2233_underuse_limits`, `tests.test_2254_radar_passes`, `tests.test_2256_fire_landing_path`, `tests.test_2273_merge_pr_required`, `tests.test_2296_admission_pression_memoire`, `tests.test_2328_ci_target_base`, `tests.test_2334_nexlink_paquet`, `tests.test_2339_autotake_migration`, `tests.test_2340_auditeur_role`, `tests.test_2345_land_retry_bound`, `tests.test_2377_revue_famille`, `tests.test_2389_fire_no_land_once`, `tests.test_2400_train_migrations`, `tests.test_2426_packet_close`, `tests.test_2432_land_head_clonebusy`, `tests.test_2442_verdict_carry`, `tests.test_2443_packet_members`, `tests.test_2446_train_assembly`, `tests.test_2446_train_initial`, `tests.test_2447_review_designation_activation`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_2473_fire_readiness_repo`, `tests.test_2481_multi_migration_reservations`, `tests.test_2526_orphan_dependance`, `tests.test_2535_fire_repo_explicite`, `tests.test_2541_force_replay_mails`, `tests.test_2542_freeze_checks`, `tests.test_2557_replay_dedup_durable`, `tests.test_2582_alerts_console_timeout`, `tests.test_2585_review_already_done`, `tests.test_2623_orphan_levee_dependance`, `tests.test_2638_reviewer_fix_family`, `tests.test_2644_prevalidation_calls`, `tests.test_2648_next_choice_underuse`, `tests.test_2681_review_reading`, `tests.test_2689_reveil_statut`, `tests.test_2702_class_at_merge_paquet`, `tests.test_2743_housekeeping_clones`, `tests.test_2759_gauge_history_isolation`, `tests.test_2776_journal_designations`, `tests.test_2786_freeze_head`, `tests.test_2789_statut_libre`, `tests.test_2808_author_at_freeze`, `tests.test_2812_superseded_corrector`, `tests.test_2813_packet_auto_merge`, `tests.test_2839_codex_account_gauges`, `tests.test_2851_alerts_court`, `tests.test_2864_proposition_familles`, `tests.test_2877_stop_sweep_without_lock`, `tests.test_2888_reviewer_fix_independence`, `tests.test_2908_train_packet_vehicle`, `tests.test_2914_train_canon_drift`, `tests.test_2928_verdict_provenance`, `tests.test_2933_housekeeping_alert_identity`, `tests.test_2934_budget_pause_notes`, `tests.test_2951_github_observation`, `tests.test_2954_incident_unique_ref`, `tests.test_2962_freeze_unresolved`, `tests.test_2971_freeze_note_marks`, `tests.test_3007_notify_fallback`, `tests.test_3035_host_reconcile`, `tests.test_3036_alert_liveness`, `tests.test_3038_mesure_cible_publiee`, `tests.test_3056_panier_3038`, `tests.test_3057_panier_vivacite`, `tests.test_3066_packet_required_checks`, `tests.test_3085_nonconclusive_gates`, `tests.test_3104_verdict_receipt`, `tests.test_3111_legacy_git_domain`, `tests.test_3114_host_wide_drain`, `tests.test_313_comptes_deux_meshes`, `tests.test_3183_review_floor`, `tests.test_3185_relit_hors_equipe`, `tests.test_3209_skipped_by_construction`, `tests.test_3221_nonconclusive_key`, `tests.test_3250_freeze_target_refresh`, `tests.test_3273_idle_prompt_pilot`, `tests.test_3308_gh_base_ref`, `tests.test_3331_canon_repo`, `tests.test_3333_blocked_freeze_holds`, `tests.test_3338_carried_corrector_trace`, `tests.test_3344_map_stale`, `tests.test_3366_train_canon_requalify`, `tests.test_3373_hold_lookalike`, `tests.test_339_orphan_settle`, `tests.test_3408_watch_foreign_pin`, `tests.test_3411_ephemeres_hors_designation`, `tests.test_3415_designation_dead_pause`, `tests.test_3416_review_correction_preview`, `tests.test_3437_debit_dependance`, `tests.test_3472_handoff_runner`, `tests.test_3498_hold_externe`, `tests.test_3512_slo_merged`, `tests.test_3530_ci_nonconclusive_dedup`, `tests.test_3532_fire_requeue_takeout`, `tests.test_3550_fire_queue_takeout_scope`, `tests.test_3556_auto_take_holds`, `tests.test_3557_review_floor_designation`, `tests.test_3558_inherited_red`, `tests.test_3572_canon_lowering_panier`, `tests.test_3578_verdict_family_receipt`, `tests.test_3592_floor_unreadable`, `tests.test_3602_manual_redesignation`, `tests.test_3611_fire_carrier_publication`, `tests.test_3631_cout_requetes`, `tests.test_3632_merge_observed_vehicle`, `tests.test_3643_host_critical_names`, `tests.test_3752_gabarit`, `tests.test_3885_fingerprints`, `tests.test_4109_vehicle_waiver`, `tests.test_4112_reservation_avant_pret`, `tests.test_4117_backlog_estimate`, `tests.test_4136_vehicule_teste`, `tests.test_420_qa_role`, `tests.test_472_zombie_turn_close`, `tests.test_501_native_host_gate`, `tests.test_503_agent_host`, `tests.test_508_runner_liveness`, `tests.test_653_db_connections`, `tests.test_711_migration_reservations`, `tests.test_743_exec_only_serve`, `tests.test_753_stop_returns_lots`, `tests.test_789_slo_alerts`, `tests.test_802_canon_sync_concurrency`, `tests.test_803_classes_famille`, `tests.test_bin_launchers`, `tests.test_catalog_store`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l118c_mail_projects`, `tests.test_l120_harness_versions`, `tests.test_l122_numeros_de_migration`, `tests.test_l132_alert_briefs`, `tests.test_l142_env_isole`, `tests.test_l148_stage1a1_alias`, `tests.test_l148_stage1a2_alias`, `tests.test_l157_estimations`, `tests.test_l157b_reference`, `tests.test_l159e6_guard_restart_alert`, `tests.test_l160f_versions`, `tests.test_l160g_migration_reservations`, `tests.test_l161f_plan`, `tests.test_l161g_ci_gel`, `tests.test_l165_election`, `tests.test_l232b_release_authority`, `tests.test_l232b_release_health`, `tests.test_l232e_drain_handoffs`, `tests.test_l234_auditor`, `tests.test_l234a_auditor_canon_guard`, `tests.test_l242b_critical_accounts`, `tests.test_l242c_critical_roles`, `tests.test_l317_project_modules`, `tests.test_l31_orphans`, `tests.test_l480_chat_veille`, `tests.test_l498_turn_cgroup`, `tests.test_l50_group_orphan_work_items`, `tests.test_l539_review_lot`, `tests.test_l56_role_executor`, `tests.test_l72_panne_de_base`, `tests.test_l744_ci_entrypoint`, `tests.test_l780_outdated_runner`, `tests.test_l795_preemption_evenement`, `tests.test_l79_chat_terminal`, `tests.test_l804_prise_executeur`, `tests.test_l93_device_state`, `tests.test_migrations`, `tests.test_migrations_additives`, `tests.test_models_entrypoint`, `tests.test_path_b_assembly`, `tests.test_process_registry`.

Modules voisins joués (support importé, carte d'impact) : carte d'impact `6d67072c3787` (lue, antérieure à la base) ; budget de carte 180 s par unité, 833 s pris, 1160 module(s) au-delà du budget non joué(s).

| unité | module | raison | fichiers de l'unité |
|---|---|---|---|
| unité 4097 | `tests.test_1022_creneau_landed` | support | `tests/test_l122_numeros_de_migration.py` |
| unité 4097 | `tests.test_2400_train_migrations` | support | `tests/test_l122_numeros_de_migration.py` |
| unité 4097 | `tests.test_2481_multi_migration_reservations` | support | `tests/test_l122_numeros_de_migration.py` |
| unité 4097 | `tests.test_3885_fingerprints` | support | `tests/test_l122_numeros_de_migration.py` |
| unité 4097 | `tests.test_711_migration_reservations` | support | `tests/test_l122_numeros_de_migration.py` |
| unité 4097 | `tests.test_2877_stop_sweep_without_lock` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_3056_panier_3038` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_753_stop_returns_lots` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_2585_review_already_done` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_3038_mesure_cible_publiee` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_l160g_migration_reservations` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_2426_packet_close` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_2102_detacher_branche` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_process_registry` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_l56_role_executor` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_l317_project_modules` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_1280_work_close_done` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_2443_packet_members` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_420_qa_role` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_l157b_reference` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_l157_estimations` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_1002_relance_perimee` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_2273_merge_pr_required` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_3066_packet_required_checks` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_1703_writer_identity` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_2212_fire_fresh_radar` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_2256_fire_landing_path` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_3308_gh_base_ref` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_3611_fire_carrier_publication` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_migrations_additives` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_path_b_assembly` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_1075_pace_multiplier` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_3558_inherited_red` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_l232b_release_health` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_3437_debit_dependance` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_catalog_store` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_l148_stage1a1_alias` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_1467_train_storage` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_3498_hold_externe` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_1561_fraicheur_releves` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_1770_prevalidation_store` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_2446_train_initial` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_2681_review_reading` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_2759_gauge_history_isolation` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_2789_statut_libre` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_2908_train_packet_vehicle` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_3643_host_critical_names` | carte | `src/ameesh/migrations.py` |
| unité 4097 | `tests.test_1127_config_explicite` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_2254_radar_passes` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_2776_journal_designations` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4097 | `tests.test_2954_incident_unique_ref` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4109 | `tests.test_3592_floor_unreadable` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_3415_designation_dead_pause` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_2541_force_replay_mails` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_2702_class_at_merge_paquet` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_2864_proposition_familles` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_3416_review_correction_preview` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_3557_review_floor_designation` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_3250_freeze_target_refresh` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_2557_replay_dedup_durable` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_2638_reviewer_fix_family` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_3602_manual_redesignation` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_2786_freeze_head` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_1002_relance_perimee` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_3273_idle_prompt_pilot` | carte | `src/ameesh/review_designation_activation.py` |
| unité 4109 | `tests.test_3631_cout_requetes` | carte | `src/ameesh/review_designation_activation.py` |
| unité 4109 | `tests.test_2256_fire_landing_path` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_2914_train_canon_drift` | carte | `src/ameesh/review_designation_activation.py` |
| unité 4109 | `tests.test_3221_nonconclusive_key` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_3530_ci_nonconclusive_dedup` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_3558_inherited_red` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_1681_incident` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_3209_skipped_by_construction` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_2446_train_initial` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_2681_review_reading` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_2908_train_packet_vehicle` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_1127_config_explicite` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_2254_radar_passes` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_2776_journal_designations` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_2954_incident_unique_ref` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_3185_relit_hors_equipe` | carte | `src/ameesh/review_designation_activation.py` |
| unité 4109 | `tests.test_803_classes_famille` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_l79_chat_terminal` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_3411_ephemeres_hors_designation` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_1400_declaration_fusion` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_1647_liste_lots_journal` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_2377_revue_famille` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_3333_blocked_freeze_holds` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_3578_verdict_family_receipt` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_2951_github_observation` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_508_runner_liveness` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_2971_freeze_note_marks` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_3550_fire_queue_takeout_scope` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_2130_d_projection` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_2473_fire_readiness_repo` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_2812_superseded_corrector` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_1022_creneau_landed` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_2334_nexlink_paquet` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_2432_land_head_clonebusy` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_2446_train_assembly` | carte | `src/ameesh/review_designation_activation.py` |
| unité 4109 | `tests.test_3111_legacy_git_domain` | carte | `src/ameesh/review_designation_activation.py`, `src/ameesh/work.py` |
| unité 4109 | `tests.test_1286_declared_merge_proof` | carte | `src/ameesh/work.py` |
| unité 4109 | `tests.test_2535_fire_repo_explicite` | carte | `src/ameesh/work.py` |
| unité 4111 | `tests.test_3512_slo_merged` | support | `tests/test_789_slo_alerts.py` |
| unité 4112 | `tests.test_3114_host_wide_drain` | support | `tests/test_503_agent_host.py` |
| unité 4112 | `tests.test_2212_fire_fresh_radar` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_2389_fire_no_land_once` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_1266_unreadable_turn_window` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_313_comptes_deux_meshes` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_path_b_assembly` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_1272_rejeu_seul_affiche` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_1466_relecture_config` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_l234_auditor` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_1681_incident` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_1467_train_service` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_501_native_host_gate` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_1672_placement_releve_perime` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_508_runner_liveness` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_653_db_connections` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_l498_turn_cgroup` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_l242c_critical_roles` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_2877_stop_sweep_without_lock` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_l780_outdated_runner` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_1467_train_events` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_l93_device_state` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_3035_host_reconcile` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_3408_watch_foreign_pin` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_2933_housekeeping_alert_identity` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_2339_autotake_migration` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_2839_codex_account_gauges` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_3007_notify_fallback` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_3472_handoff_runner` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_1580_marge_forfait` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_472_zombie_turn_close` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_l31_orphans` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_753_stop_returns_lots` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_bin_launchers` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_1580_widget_forfait` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_2743_housekeeping_clones` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_2623_orphan_levee_dependance` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_l242b_critical_accounts` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_339_orphan_settle` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_1349_agent_peek` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_l165_election` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_l120_harness_versions` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_l804_prise_executeur` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_2096_runner_memoire` | carte | `src/ameesh/runner.py` |
| unité 4112 | `tests.test_3373_hold_lookalike` | carte | `src/ameesh/runner.py` |
| unité 4136 | `tests.test_2328_ci_target_base` | support | `tests/test_l161g_ci_gel.py` |
| unité 4136 | `tests.test_2644_prevalidation_calls` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4136 | `tests.test_2813_packet_auto_merge` | carte | `src/ameesh/ci.py`, `src/ameesh/prevalidation_calls.py` |
| unité 4136 | `tests.test_2962_freeze_unresolved` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4136 | `tests.test_2442_verdict_carry` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4136 | `tests.test_2542_freeze_checks` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4136 | `tests.test_2447_review_designation_activation` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4136 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4117 | `tests.test_1002_relance_perimee` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_path_b_assembly` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1075_pace_multiplier` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2022_fleet` | carte | `src/ameesh/backlog.py` |
| unité 4117 | `tests.test_l148_stage1a2_alias` | carte | `src/ameesh/backlog.py` |
| unité 4117 | `tests.test_models_entrypoint` | carte | `src/ameesh/backlog.py` |
| unité 4117 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_l148_stage1a1_alias` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1467_train_storage` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2446_train_initial` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2681_review_reading` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2908_train_packet_vehicle` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1127_config_explicite` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2254_radar_passes` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2776_journal_designations` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2954_incident_unique_ref` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3185_relit_hors_equipe` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_803_classes_famille` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_l132_alert_briefs` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_l79_chat_terminal` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1680_packet_reservation` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3411_ephemeres_hors_designation` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1400_declaration_fusion` | carte | `src/ameesh/backlog.py` |
| unité 4117 | `tests.test_1647_liste_lots_journal` | carte | `src/ameesh/backlog.py`, `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py` … |
| unité 4117 | `tests.test_2377_revue_famille` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3333_blocked_freeze_holds` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3578_verdict_family_receipt` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2951_github_observation` | carte | `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_508_runner_liveness` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2971_freeze_note_marks` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3550_fire_queue_takeout_scope` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2130_d_projection` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_l161g_ci_gel` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1467_train_fire` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1770_prevalidation_cli` | carte | `src/ameesh/backlog.py` |
| unité 4117 | `tests.test_2812_superseded_corrector` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_743_exec_only_serve` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1022_creneau_landed` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1467_train_cli` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2334_nexlink_paquet` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3057_panier_vivacite` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3111_legacy_git_domain` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3036_alert_liveness` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3344_map_stale` | carte | `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2877_stop_sweep_without_lock` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_l780_outdated_runner` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1467_train_runtime` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3104_verdict_receipt` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3592_floor_unreadable` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1901_gate_unreadable` | carte | `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2888_reviewer_fix_independence` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3338_carried_corrector_trace` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3415_designation_dead_pause` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_l744_ci_entrypoint` | carte | `src/ameesh/backlog.py` |
| unité 4117 | `tests.test_1053_famille_au_gel` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2928_verdict_provenance` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3366_train_canon_requalify` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/backlog.py`, `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py` … |
| unité 4117 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/backlog.py`, `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py` … |
| unité 4117 | `tests.test_3056_panier_3038` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_l160f_versions` | carte | `src/ameesh/backlog.py`, `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py` … |
| unité 4117 | `tests.test_1467_train_events` | carte | `src/ameesh/backlog.py`, `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py` … |
| unité 4117 | `tests.test_2211_roadmap_followups` | carte | `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_l795_preemption_evenement` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_802_canon_sync_concurrency` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_l480_chat_veille` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3183_review_floor` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3572_canon_lowering_panier` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3532_fire_requeue_takeout` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_l539_review_lot` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_l142_env_isole` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1227_suite_1176` | carte | `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2808_author_at_freeze` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_l232e_drain_handoffs` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3331_canon_repo` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3408_watch_foreign_pin` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/backlog.py`, `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py` … |
| unité 4117 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_3556_auto_take_holds` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/backlog.py`, `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py` … |
| unité 4117 | `tests.test_2400_train_migrations` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2851_alerts_court` | carte | `src/ameesh/backlog.py`, `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py` … |
| unité 4117 | `tests.test_2339_autotake_migration` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_l232b_release_authority` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_1391_backlog_route` | carte | `src/ameesh/backlog.py`, `src/ameesh/estimates.py`, `src/ameesh/storage/interface.py` … |
| unité 4117 | `tests.test_2345_land_retry_bound` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2541_force_replay_mails` | carte | `src/ameesh/backlog.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4117 | `tests.test_2648_next_choice_underuse` | carte | `src/ameesh/storage/interface.py` |
| unité 4117 | `tests.test_3066_packet_required_checks` | carte | `src/ameesh/storage/interface.py` |
| unité 4117 | `tests.test_2233_underuse_limits` | carte | `src/ameesh/storage/interface.py` |
| unité 4117 | `tests.test_3308_gh_base_ref` | carte | `src/ameesh/storage/interface.py` |
| unité 4117 | `tests.test_2022_host_zombies` | carte | `src/ameesh/storage/interface.py` |
| unité 4117 | `tests.test_313_comptes_deux_meshes` | carte | `src/ameesh/storage/interface.py` |
| unité 4117 | `tests.test_1318_verdict_perime` | carte | `src/ameesh/estimates.py` |
| unité 4117 | `tests.test_l159e6_guard_restart_alert` | carte | `src/ameesh/storage/interface.py` |
| unité 4117 | `tests.test_l234_auditor` | carte | `src/ameesh/backlog.py` |
| unité 4117 | `tests.test_l234a_auditor_canon_guard` | carte | `src/ameesh/backlog.py` |
| unité 4117 | `tests.test_3437_debit_dependance` | carte | `src/ameesh/storage/interface.py` |

Non concluants (canon §3 : ne retiennent ni paquet ni release) :

- NON CONCLUANT : unité 4097 — modules tests.test_l122_numeros_de_migration, tests.test_1022_creneau_landed, tests.test_2400_train_migrations, tests.test_2481_multi_migration_reservations, tests.test_3885_fingerprints, tests.test_711_migration_reservations, tests.test_2877_stop_sweep_without_lock, tests.test_3056_panier_3038, tests.test_1425_stop_sweep_suites, tests.test_753_stop_returns_lots, tests.test_2585_review_already_done, tests.test_3038_mesure_cible_publiee, tests.test_l160g_migration_reservations, tests.test_2426_packet_close, tests.test_2102_detacher_branche, tests.test_process_registry, tests.test_l56_role_executor, tests.test_l317_project_modules, tests.test_1280_work_close_done, tests.test_2443_packet_members, tests.test_420_qa_role, tests.test_l157b_reference, tests.test_l157_estimations, tests.test_1002_relance_perimee, tests.test_2273_merge_pr_required, tests.test_3066_packet_required_checks, tests.test_1703_writer_identity, tests.test_2212_fire_fresh_radar, tests.test_2256_fire_landing_path, tests.test_3308_gh_base_ref, tests.test_3611_fire_carrier_publication, tests.test_migrations_additives, tests.test_path_b_assembly, tests.test_1075_pace_multiplier, tests.test_3558_inherited_red, tests.test_l232b_release_health, tests.test_2167_host_housekeeping, tests.test_3437_debit_dependance, tests.test_catalog_store, tests.test_l148_stage1a1_alias, tests.test_1467_train_storage, tests.test_3085_nonconclusive_gates, tests.test_3498_hold_externe, tests.test_1561_fraicheur_releves, tests.test_1770_prevalidation_store, tests.test_2446_train_initial, tests.test_2681_review_reading, tests.test_2759_gauge_history_isolation, tests.test_2789_statut_libre, tests.test_2908_train_packet_vehicle, tests.test_3643_host_critical_names, tests.test_1127_config_explicite, tests.test_2254_radar_passes, tests.test_2776_journal_designations, tests.test_2954_incident_unique_ref — psql: error — extrait : ameesh.db.Unavailable: psycopg: connection failed: connection to server at "127.0.0.1", port 55432 failed: fe_sendauth: no password supplied; psql: psql: error: connection to server at "127.0.0.1", port 55432 failed: fe_sendauth: no passwor

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
