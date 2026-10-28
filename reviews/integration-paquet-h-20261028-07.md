# Paquet integration/paquet-h-20261028-07 — composition (§12), sur main `1d3c6d22cf66`

Base : `main` **`1d3c6d22cf6681668cf30473c2aaeafd50e4269d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261028-07`, cible `main`.
Porteur : **#4243**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (24 unités, 24 lots)

| lot | source composée |
|---|---|
| #4231 | `a71d03485b4a90ebd593e12e0790c84a6446dae3` |
| #4245 | `67ff7a091a07412e4c65370b2b9d9de54a6318b2` |
| #4271 | `a24f2a374013b32f1f2f96e787b1927d8f934fe9` |
| #4272 | `b037433738303621afdf32b745ed4cc36a43fa23` |
| #4278 | `d8a32635c9ada1938cc7aefc623f99418fed5afe` |
| #4150 | `d05798a03b2ca83bb7b5ac857c71618644c6289d` |
| #4172 | `11009daa0891dbcba6ba108a8ef12a080669e57d` |
| #4173 | `4d17c0c06c9eec69cc628ee09690db907f65bdfd` |
| #4219 | `3f3e44c8d643062bec6dce853653835e1fd29add` |
| #4222 | `f167e1e9da69ca75ca22cb7a5199e041bf1aa815` |
| #4234 | `f42dd173ffbb090d5b6589caa1ba4456e792af72` |
| #4239 | `aafff3ba42be27d90c030bbd5be760de771c2b88` |
| #4241 | `2a37f198b450ee796c7169449b060d8fb92a5f15` |
| #4242 | `80925d7e92ea45a8d5f5956c3705845b4975ce81` |
| #4253 | `a4399dd890617d770e6c949ba14d907be4d0d387` |
| #4259 | `de81f390b849ab66e9e72092e0533c503c47aef6` |
| #4266 | `06d8bfc336bddc2f85c1472af1c42c1bc63fbea1` |
| #4290 | `b4d1dd31a0edc02a9865a159223b283a863df412` |
| #4294 | `a0bdc63a70ec383e67edc7962951fc9f271b88f4` |
| #4304 | `ededf13ca6ea508815e19a92a6dc0cc1e6c63eb9` |
| #4308 | `e9850a0953451681535b773d72ea90ff67c774ce` |
| #4185 | `5d7afd1bfa9217ed7c1e1ff23efba7dd65694740` |
| #4235 | `cf0ccd44703a6a8a440c7b0fd7e1bf1fbeb96adb` |
| #4240 | `ccada42b893d22ace21f838f306163da0c525f33` |

## Composition (24 unités, 24 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4231 | `a71d03485b4a` | `5faa3a0513cd` | composition |
| 2 | #4245 | `67ff7a091a07` | `11566dc040e6` | composition |
| 3 | #4271 | `a24f2a374013` | `ffbd2a872795` | composition |
| 4 | #4272 | `b03743373830` | `4b8ee1a7645a` | composition |
| 5 | #4278 | `d8a32635c9ad` | `0df318d678c9` | composition |
| 6 | #4150 | `d05798a03b2c` | `07bba002d34d` | composition |
| 7 | #4172 | `11009daa0891` | `830a6d380a90` | composition |
| 8 | #4173 | `4d17c0c06c9e` | `05718a4c5b62` | composition |
| 9 | #4219 | `3f3e44c8d643` | `0043e9d1c4fb` | composition |
| 10 | #4222 | `f167e1e9da69` | `c8dda446e9aa` | composition |
| 11 | #4234 | `f42dd173ffbb` | `5e07b4ebd5f4` | composition |
| 12 | #4239 | `aafff3ba42be` | `c6ecd0442832` | composition |
| 13 | #4241 | `2a37f198b450` | `fc10283fdfe8` | composition |
| 14 | #4242 | `80925d7e92ea` | `63d4181ace45` | composition |
| 15 | #4253 | `a4399dd89061` | `6dfc8400676d` | composition |
| 16 | #4259 | `de81f390b849` | `524bc2dd3027` | composition |
| 17 | #4266 | `06d8bfc336bd` | `19fafb21cf73` | composition |
| 18 | #4290 | `b4d1dd31a0ed` | `1569a5309d04` | composition |
| 19 | #4294 | `a0bdc63a70ec` | `c9d04dd2b55a` | composition |
| 20 | #4304 | `ededf13ca6ea` | `37936897e437` | composition |
| 21 | #4308 | `e9850a095345` | `964a0ad5188c` | composition |
| 22 | #4185 | `5d7afd1bfa92` | `b0fff6837fa6` | composition |
| 23 | #4235 | `cf0ccd44703a` | `fe6a00c5d095` | composition |
| 24 | #4240 | `ccada42b893d` | `aa79d9a6fc24` | composition |

## Écartés

| lot | motif |
|---|---|
| #4174 | conflit, coupure suivante — fichiers : docs/WORK-DEPENDENCIES.md |
| #4217 | conflit, coupure suivante — fichiers : scripts/paquet-auto.py |
| #4237 | conflit, coupure suivante — fichiers : scripts/paquet-auto.py, scripts/paquet-horaire.py |
| #4248 | conflit, coupure suivante — fichiers : deploy/mise-a-jour/vm.sh |
| #4295 | conflit, coupure suivante — fichiers : tests/test_3802_host_pass_lock.py |

Liste calculée : 29 lot(s) prêt(s), 10 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 18.2 s.
- Tests locaux : 22 séries, 378 modules, 525 tests exécutés, 0 échec(s), 0 run(s) rouge(s) sans effet, 16 non concluant(s), 26 sauté(s) : `tests.test_1002_relance_perimee`, `tests.test_1003_merge_pr`, `tests.test_1021_tenue_attribution`, `tests.test_1022_creneau_landed`, `tests.test_1037_bascule_harnais`, `tests.test_1053_famille_au_gel`, `tests.test_1075_pace_multiplier`, `tests.test_1127_config_explicite`, `tests.test_1132_jauges_perimees`, `tests.test_1179_sonde_pgbouncer`, `tests.test_1190_dryrun_cross_cache_inode`, `tests.test_1193_suite_dependency`, `tests.test_1227_suite_1176`, `tests.test_1266_unreadable_turn_window`, `tests.test_1272_rejeu_seul_affiche`, `tests.test_1280_work_close_done`, `tests.test_1323_actif`, `tests.test_1349_agent_peek`, `tests.test_1391_backlog_route`, `tests.test_1398_peek_boundaries`, `tests.test_1400_declaration_fusion`, `tests.test_1425_stop_sweep_suites`, `tests.test_1462_operator_pause`, `tests.test_1465_provenance`, `tests.test_1466_config_profil`, `tests.test_1466_pause_longue`, `tests.test_1466_relecture_config`, `tests.test_1467_train`, `tests.test_1467_train_cli`, `tests.test_1467_train_events`, `tests.test_1467_train_fire`, `tests.test_1467_train_history`, `tests.test_1467_train_policy`, `tests.test_1467_train_runtime`, `tests.test_1467_train_service`, `tests.test_1467_train_storage`, `tests.test_1561_fraicheur_releves`, `tests.test_1580_marge_forfait`, `tests.test_1580_widget_forfait`, `tests.test_1644_paquet_inclusion`, `tests.test_1645_github_identity`, `tests.test_1647_liste_lots_journal`, `tests.test_1672_placement_releve_perime`, `tests.test_1677_prise_empilee`, `tests.test_1680_packet_reservation`, `tests.test_1681_incident`, `tests.test_1702_jauges_n`, `tests.test_1770_mechanics_prevalidation`, `tests.test_1770_prevalidation_cli`, `tests.test_1770_prevalidation_store`, `tests.test_1883_overage_boucle`, `tests.test_1901_gate_unreadable`, `tests.test_2003_reap_volumes`, `tests.test_2009_sigterm_demarrage`, `tests.test_2022_disk_early_warning`, `tests.test_2022_fleet`, `tests.test_2022_host_zombies`, `tests.test_2096_runner_memoire`, `tests.test_2130_d_projection`, `tests.test_2167_host_housekeeping`, `tests.test_2180_housekeeping_discovery`, `tests.test_2200_pause_visible`, `tests.test_2211_roadmap_followups`, `tests.test_2212_fire_fresh_radar`, `tests.test_2233_underuse_limits`, `tests.test_2254_radar_passes`, `tests.test_2256_fire_landing_path`, `tests.test_2296_admission_pression_memoire`, `tests.test_2334_nexlink_paquet`, `tests.test_2339_autotake_migration`, `tests.test_2340_auditeur_role`, `tests.test_2354_swap_sans_psi`, `tests.test_2377_revue_famille`, `tests.test_2389_fire_no_land_once`, `tests.test_2426_packet_close`, `tests.test_2442_verdict_carry`, `tests.test_2443_packet_members`, `tests.test_2446_train_assembly`, `tests.test_2446_train_initial`, `tests.test_2448_release_cutoff`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_246_btrfs_guard`, `tests.test_2481_multi_migration_reservations`, `tests.test_2487_designation_carry`, `tests.test_2526_orphan_dependance`, `tests.test_2542_freeze_checks`, `tests.test_2552_revue_source`, `tests.test_2556_tmp_copies_once`, `tests.test_2557_replay_dedup_durable`, `tests.test_2580_partage_comptes`, `tests.test_2582_alerts_console_timeout`, `tests.test_2584_backlog_panier`, `tests.test_2601_packet_watch`, `tests.test_2602_event_dedupe`, `tests.test_2610_rotation_tmpdir`, `tests.test_2623_orphan_levee_dependance`, `tests.test_2638_reviewer_fix_family`, `tests.test_2644_prevalidation_calls`, `tests.test_2648_next_choice_underuse`, `tests.test_2681_review_reading`, `tests.test_2689_reveil_statut`, `tests.test_2743_housekeeping_clones`, `tests.test_2759_gauge_history_isolation`, `tests.test_2776_journal_designations`, `tests.test_2786_freeze_head`, `tests.test_2789_statut_libre`, `tests.test_2812_superseded_corrector`, `tests.test_2813_packet_auto_merge`, `tests.test_2814_pressure_cgroup_capacity`, `tests.test_2820_events_rotation`, `tests.test_2827_housekeeping_local_refs`, `tests.test_2839_codex_account_gauges`, `tests.test_2845_github_watch`, `tests.test_2851_alerts_court`, `tests.test_2864_proposition_familles`, `tests.test_2873_freeze_gate`, `tests.test_2877_stop_sweep_without_lock`, `tests.test_2879_alerts_disk_trend`, `tests.test_2888_reviewer_fix_independence`, `tests.test_288_cost_per_message`, `tests.test_2905_stale_spend_key`, `tests.test_2908_train_packet_vehicle`, `tests.test_2928_verdict_provenance`, `tests.test_2931_packet_target`, `tests.test_2933_housekeeping_alert_identity`, `tests.test_2934_budget_pause_notes`, `tests.test_2951_github_observation`, `tests.test_2954_incident_unique_ref`, `tests.test_2954_work_set_type`, `tests.test_2962_freeze_unresolved`, `tests.test_2971_freeze_note_marks`, `tests.test_3007_notify_fallback`, `tests.test_3014_role_run`, `tests.test_3035_host_reconcile`, `tests.test_3036_alert_liveness`, `tests.test_3056_panier_3038`, `tests.test_3057_panier_vivacite`, `tests.test_3066_packet_required_checks`, `tests.test_3085_nonconclusive_gates`, `tests.test_3104_verdict_receipt`, `tests.test_3111_legacy_git_domain`, `tests.test_3114_host_wide_drain`, `tests.test_3115_gate_hold_lease_ttl`, `tests.test_3123_volatile_hold`, `tests.test_313_comptes_deux_meshes`, `tests.test_3180_host_cli_actor`, `tests.test_3185_relit_hors_equipe`, `tests.test_3186_unconcluded_aggregate`, `tests.test_3209_skipped_by_construction`, `tests.test_3218_grant_latency`, `tests.test_3221_nonconclusive_key`, `tests.test_3226_dsh_scopes`, `tests.test_3236_brief_fenetre`, `tests.test_3244_release_optional_jobs`, `tests.test_3273_idle_prompt_pilot`, `tests.test_3277_tmp_over_quota`, `tests.test_3288_fixes_owed`, `tests.test_3291_staging_pending`, `tests.test_3292_debit`, `tests.test_3293_cache_pressure`, `tests.test_3307_basket_3300`, `tests.test_3308_gh_base_ref`, `tests.test_3327_role_timers`, `tests.test_3333_blocked_freeze_holds`, `tests.test_3335_corrector_survival`, `tests.test_3337_presence_hors_executeur`, `tests.test_3344_map_stale`, `tests.test_3345_profils_runner`, `tests.test_3346_masquage_secrets_doctor`, `tests.test_3366_train_canon_requalify`, `tests.test_3373_hold_lookalike`, `tests.test_3379_argparse_help`, `tests.test_3383_alerts_role_timer`, `tests.test_3396_housekeeping_unreadable`, `tests.test_3399_dangling_supersedes`, `tests.test_339_orphan_settle`, `tests.test_3402_alerts_advisory`, `tests.test_3408_watch_foreign_pin`, `tests.test_3411_ephemeres_hors_designation`, `tests.test_3415_designation_dead_pause`, `tests.test_3416_review_correction_preview`, `tests.test_3437_debit_dependance`, `tests.test_3447_prise_externe`, `tests.test_3451_courrier_info`, `tests.test_3460_agent_retire`, `tests.test_3472_handoff_runner`, `tests.test_3480_ephemeres_hors_prise`, `tests.test_3483_train_canon_auto`, `tests.test_3495_dead_mail`, `tests.test_3498_hold_externe`, `tests.test_3503_jit`, `tests.test_3509_local_handoff`, `tests.test_3512_slo_merged`, `tests.test_3526_runtime_seal`, `tests.test_3527_release_identity`, `tests.test_3529_nexlink_release`, `tests.test_3530_ci_nonconclusive_dedup`, `tests.test_3532_fire_requeue_takeout`, `tests.test_3535_flux_tolerance`, `tests.test_3550_fire_queue_takeout_scope`, `tests.test_3556_auto_take_holds`, `tests.test_3558_inherited_red`, `tests.test_3565_hold_race`, `tests.test_3568_alerts_delegate`, `tests.test_3572_canon_lowering_panier`, `tests.test_3575_alerts_basket`, `tests.test_3578_verdict_family_receipt`, `tests.test_3589_stacked_freeze`, `tests.test_3592_floor_unreadable`, `tests.test_3619_host_crypto`, `tests.test_3632_merge_observed_vehicle`, `tests.test_3643_host_critical_names`, `tests.test_3752_gabarit`, `tests.test_3770_handoff_child_stdin`, `tests.test_3802_host_pass_lock`, `tests.test_3807_paquet_auto`, `tests.test_3835_prior_freeze_timeout`, `tests.test_3931_paquet_rouge_cible`, `tests.test_4007_revision_transfer`, `tests.test_4113_packet_epoch`, `tests.test_4134_process_admission`, `tests.test_4150_host_idle`, `tests.test_4152_paquet_test_logs`, `tests.test_4172_backlog_coherence`, `tests.test_4173_empreinte`, `tests.test_4219_release_report`, `tests.test_4231_unready_holder`, `tests.test_4234_housekeeping_muet`, `tests.test_4235_hold_fenetre`, `tests.test_4245_lignee_meme_hote`, `tests.test_4253_reservation_reprise_longue`, `tests.test_4259_client_literals`, `tests.test_4271_paquet_series`, `tests.test_4304_paquet_poussee_precoce`, `tests.test_435_agent_projects`, `tests.test_468_agent_naming`, `tests.test_472_zombie_turn_close`, `tests.test_500_eviction_irremovable`, `tests.test_501_native_host_gate`, `tests.test_501_unleased_registry`, `tests.test_503_agent_host`, `tests.test_508_runner_liveness`, `tests.test_530_housekeeping_followups`, `tests.test_532_pgbouncer_pool`, `tests.test_653_db_connections`, `tests.test_706_rotation_latency`, `tests.test_743_exec_only_serve`, `tests.test_753_stop_returns_lots`, `tests.test_762_menage_dryrun_episode`, `tests.test_802_canon_sync_concurrency`, `tests.test_803_classes_famille`, `tests.test_810_multi_project_consumers`, `tests.test_898_pause_rythme`, `tests.test_approve_equipes`, `tests.test_bin_launchers`, `tests.test_bout_en_bout`, `tests.test_catalog_store`, `tests.test_cluster`, `tests.test_cost`, `tests.test_events_attach`, `tests.test_generic_core`, `tests.test_l107_executor_contract`, `tests.test_l117_account_spread`, `tests.test_l120_harness_versions`, `tests.test_l126_issues`, `tests.test_l132_alert_briefs`, `tests.test_l142_env_isole`, `tests.test_l148_stage1a1_alias`, `tests.test_l148_stage1a2_alias`, `tests.test_l148_stage3_workstream`, `tests.test_l148_stage3b_workstream_readers`, `tests.test_l157_estimations`, `tests.test_l159c_plafonds_memoire`, `tests.test_l159d_host_measure`, `tests.test_l159e6_guard_restart_alert`, `tests.test_l159e_guard`, `tests.test_l159i_vm_recipe`, `tests.test_l160b_radar`, `tests.test_l160f_versions`, `tests.test_l160g_migration_reservations`, `tests.test_l1616_mechanics`, `tests.test_l161f_plan`, `tests.test_l161g_ci_gel`, `tests.test_l162_tmpfs`, `tests.test_l165_election`, `tests.test_l165_ep2_election_score`, `tests.test_l165_ep3_election_standby`, `tests.test_l165_ep4_pilot_gesture`, `tests.test_l1757_paquets_horaires`, `tests.test_l219_housekeeping_tmp`, `tests.test_l221_cout_result_sans_usage`, `tests.test_l230_build_artifacts`, `tests.test_l232b_bootstrap`, `tests.test_l232b_bootstrap_installer`, `tests.test_l232b_entree_operateur`, `tests.test_l232b_migration_worker`, `tests.test_l232d_login_e2e`, `tests.test_l232e_drain_handoffs`, `tests.test_l232e_host_lifecycle`, `tests.test_l233_decide_only`, `tests.test_l233b_budget_handoff`, `tests.test_l234_auditor`, `tests.test_l242b2_budget`, `tests.test_l242b_critical_accounts`, `tests.test_l242c_critical_roles`, `tests.test_l248b_deps_packagers`, `tests.test_l253_disk_volumes`, `tests.test_l317_project_modules`, `tests.test_l317_proof_helper`, `tests.test_l317_snapshot_limits`, `tests.test_l317b_module_audit`, `tests.test_l317c_deep_analysis`, `tests.test_l31_admission`, `tests.test_l31_orphans`, `tests.test_l31_pressure`, `tests.test_l31_relocation`, `tests.test_l31_resources`, `tests.test_l31_visibility`, `tests.test_l36_vivacite`, `tests.test_l411_docker_storage`, `tests.test_l4264_canon_check_registre`, `tests.test_l42_plusieurs_canons`, `tests.test_l43_perimetre_hote`, `tests.test_l46_corrections`, `tests.test_l480_chat_veille`, `tests.test_l490_deepseek_cgroup`, `tests.test_l498_turn_cgroup`, `tests.test_l50_group_orphan_work_items`, `tests.test_l513_deepseek_offpeak`, `tests.test_l51_persona`, `tests.test_l539_review_lot`, `tests.test_l53_session_backup`, `tests.test_l540_team_queue`, `tests.test_l56_role_executor`, `tests.test_l59_activity`, `tests.test_l64_unmeasured`, `tests.test_l66_desktop`, `tests.test_l71_comptabilite`, `tests.test_l72_panne_de_base`, `tests.test_l73_housekeeping`, `tests.test_l744_ci_entrypoint`, `tests.test_l77_resident_queue`, `tests.test_l780_outdated_runner`, `tests.test_l78_conversations`, `tests.test_l790_outdated_restart`, `tests.test_l795_preemption_evenement`, `tests.test_l79_chat_terminal`, `tests.test_l804_prise_executeur`, `tests.test_l83_chat_api`, `tests.test_l848_github_quota`, `tests.test_l84_serve`, `tests.test_l85_events`, `tests.test_l860_github_read`, `tests.test_l87_serve_actions`, `tests.test_l87c_alert_ack`, `tests.test_l88_ui`, `tests.test_l93_device_state`, `tests.test_l94_underuse`, `tests.test_l95_budget_juste`, `tests.test_l98_modules_registry`, `tests.test_mail`, `tests.test_migrations`, `tests.test_migrations_vues`, `tests.test_models_entrypoint`, `tests.test_path_b_assembly`, `tests.test_placement`, `tests.test_pressure_orchestrator`, `tests.test_process_approvals`, `tests.test_process_approvals_704`, `tests.test_process_card`, `tests.test_process_registry`, `tests.test_process_verifiers`, `tests.test_process_verifiers_seam`, `tests.test_rename`, `tests.test_tube_ferme`, `tests.test_version`, `tests.test_work`.

Modules voisins joués (support importé, carte d'impact) : carte d'impact `6d67072c3787` (périmée, antérieure à la base) ; budget de carte 180 s par unité, 2452 s pris, 1975 module(s) au-delà du budget non joué(s).

| unité | module | raison | fichiers de l'unité |
|---|---|---|---|
| unité 4231 | `tests.test_1272_rejeu_seul_affiche` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_1467_train_service` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2212_fire_fresh_radar` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2389_fire_no_land_once` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_3273_idle_prompt_pilot` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_706_rotation_latency` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_1266_unreadable_turn_window` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_1466_relecture_config` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_1681_incident` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_path_b_assembly` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_313_comptes_deux_meshes` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_501_native_host_gate` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l234_auditor` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l53_session_backup` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l77_resident_queue` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_1672_placement_releve_perime` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2933_housekeeping_alert_identity` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_3307_basket_3300` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l498_turn_cgroup` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l780_outdated_runner` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2339_autotake_migration` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2877_stop_sweep_without_lock` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_508_runner_liveness` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_bin_launchers` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_472_zombie_turn_close` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_3408_watch_foreign_pin` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l242c_critical_roles` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_288_cost_per_message` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2096_runner_memoire` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_1580_marge_forfait` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_653_db_connections` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l93_device_state` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_3035_host_reconcile` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_1349_agent_peek` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_1465_provenance` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_1467_train_events` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_1580_widget_forfait` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l31_orphans` | carte | `src/ameesh/election.py`, `src/ameesh/runner.py` |
| unité 4231 | `tests.test_3472_handoff_runner` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l120_harness_versions` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l795_preemption_evenement` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l232b_bootstrap_installer` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2905_stale_spend_key` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_339_orphan_settle` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_3007_notify_fallback` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2839_codex_account_gauges` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l804_prise_executeur` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_1466_pause_longue` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_3373_hold_lookalike` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_753_stop_returns_lots` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_501_unleased_registry` | carte | `src/ameesh/election.py`, `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l165_election` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l31_resources` | carte | `src/ameesh/election.py`, `src/ameesh/runner.py` |
| unité 4231 | `tests.test_3288_fixes_owed` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_3451_courrier_info` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l790_outdated_restart` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l490_deepseek_cgroup` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2610_rotation_tmpdir` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2820_events_rotation` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l165_ep2_election_score` | carte | `src/ameesh/election.py`, `src/ameesh/runner.py` |
| unité 4231 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l242b_critical_accounts` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l317c_deep_analysis` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l84_serve` | carte | `src/ameesh/runner.py` |
| unité 4231 | `tests.test_l165_ep3_election_standby` | carte | `src/ameesh/election.py`, `src/ameesh/runner.py` |
| unité 4245 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_1467_train_runtime` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_3115_gate_hold_lease_ttl` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_3123_volatile_hold` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_3565_hold_race` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_2180_housekeeping_discovery` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_1391_backlog_route` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_l165_ep4_pilot_gesture` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_l165_ep2_election_score` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_l46_corrections` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_1467_train_history` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_1193_suite_dependency` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_1280_work_close_done` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_l157_estimations` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_l540_team_queue` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_1677_prise_empilee` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_l36_vivacite` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_l233_decide_only` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_1003_merge_pr` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_1467_train` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_2443_packet_members` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_3066_packet_required_checks` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_3308_gh_base_ref` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_2256_fire_landing_path` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_3558_inherited_red` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_2776_journal_designations` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_2908_train_packet_vehicle` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4245 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/pilot_gesture.py` |
| unité 4271 | `tests.test_2457_garde_non_python` | support | `tests/test_2449_garde_mecanique.py` |
| unité 4271 | `tests.test_1272_rejeu_seul_affiche` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_1681_incident` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_1770_mechanics_prevalidation` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_1672_placement_releve_perime` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_bin_launchers` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_472_zombie_turn_close` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_3592_floor_unreadable` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_3408_watch_foreign_pin` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_l1616_mechanics` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_3416_review_correction_preview` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2584_backlog_panier` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_288_cost_per_message` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_l1757_paquets_horaires` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2096_runner_memoire` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_1580_marge_forfait` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_1467_train_policy` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_1349_agent_peek` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_1467_train_events` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_1580_widget_forfait` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2931_packet_target` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_3415_designation_dead_pause` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_3447_prise_externe` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_l31_orphans` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_l120_harness_versions` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_l71_comptabilite` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2354_swap_sans_psi` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2905_stale_spend_key` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_l253_disk_volumes` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2446_train_assembly` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2864_proposition_familles` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_l95_budget_juste` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2839_codex_account_gauges` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_l64_unmeasured` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_1466_pause_longue` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_1391_backlog_route` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2200_pause_visible` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2638_reviewer_fix_family` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_3373_hold_lookalike` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_l159d_host_measure` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_501_unleased_registry` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_3527_release_identity` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_work` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_l31_resources` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_2557_replay_dedup_durable` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_3288_fixes_owed` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_3451_courrier_info` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_l317_project_modules` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_3572_canon_lowering_panier` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_l790_outdated_restart` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_l160b_radar` | carte | `src/ameesh/mechanics.py` |
| unité 4271 | `tests.test_3186_unconcluded_aggregate` | carte | `src/ameesh/mechanics.py` |
| unité 4150 | `tests.test_1349_agent_peek` | support | `tests/test_l84_serve.py` |
| unité 4150 | `tests.test_l85_events` | support | `tests/test_l84_serve.py` |
| unité 4150 | `tests.test_l87_serve_actions` | support | `tests/test_l84_serve.py` |
| unité 4150 | `tests.test_l88_ui` | support | `tests/test_l84_serve.py` |
| unité 4150 | `tests.test_1002_relance_perimee` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1466_config_profil` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2212_fire_fresh_radar` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_3346_masquage_secrets_doctor` | carte | `src/ameesh/cli.py`, `src/ameesh/config.py` |
| unité 4150 | `tests.test_1075_pace_multiplier` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1466_relecture_config` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_1561_fraicheur_releves` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2022_fleet` | carte | `src/ameesh/mesh_cli.py` |
| unité 4150 | `tests.test_2022_host_zombies` | carte | `src/ameesh/config.py`, `src/ameesh/resources.py` |
| unité 4150 | `tests.test_3437_debit_dependance` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3498_hold_externe` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_313_comptes_deux_meshes` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_3643_host_critical_names` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_501_native_host_gate` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_l107_executor_contract` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l160f_versions` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_version` | carte | `src/ameesh/cli.py`, `src/ameesh/config.py` |
| unité 4150 | `tests.test_2681_review_reading` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3619_host_crypto` | carte | `src/ameesh/cli.py`, `src/ameesh/config.py` |
| unité 4150 | `tests.test_898_pause_rythme` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_catalog_store` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l159i_vm_recipe` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1467_train_storage` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1672_placement_releve_perime` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_1770_prevalidation_store` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2580_partage_comptes` | carte | `src/ameesh/config.py`, `src/ameesh/mesh_cli.py` |
| unité 4150 | `tests.test_2776_journal_designations` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3185_relit_hors_equipe` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l148_stage3_workstream` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l79_chat_terminal` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2759_gauge_history_isolation` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2789_statut_libre` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2908_train_packet_vehicle` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2933_housekeeping_alert_identity` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_l148_stage1a1_alias` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l148_stage3b_workstream_readers` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_models_entrypoint` | carte | `src/ameesh/config.py`, `src/ameesh/mesh_cli.py` |
| unité 4150 | `tests.test_1227_suite_1176` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1702_jauges_n` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_2879_alerts_disk_trend` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3292_debit` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3333_blocked_freeze_holds` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3411_ephemeres_hors_designation` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l221_cout_result_sans_usage` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l780_outdated_runner` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_2254_radar_passes` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2339_autotake_migration` | carte | `src/ameesh/config.py`, `src/ameesh/resources.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_3293_cache_pressure` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3345_profils_runner` | carte | `src/ameesh/cli.py`, `src/ameesh/config.py` |
| unité 4150 | `tests.test_l117_account_spread` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2812_superseded_corrector` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2877_stop_sweep_without_lock` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_2888_reviewer_fix_independence` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3550_fire_queue_takeout_scope` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_508_runner_liveness` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_l31_admission` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l480_chat_veille` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1132_jauges_perimees` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2377_revue_famille` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3180_host_cli_actor` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_803_classes_famille` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_bin_launchers` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_l132_alert_briefs` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1467_train_fire` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2951_github_observation` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3014_role_run` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3344_map_stale` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_472_zombie_turn_close` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_l142_env_isole` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l159e_guard` | carte | `src/ameesh/resources.py` |
| unité 4150 | `tests.test_2446_train_initial` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2851_alerts_court` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3057_panier_vivacite` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3111_legacy_git_domain` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3556_auto_take_holds` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3592_floor_unreadable` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/config.py`, `src/ameesh/mesh_cli.py` |
| unité 4150 | `tests.test_2334_nexlink_paquet` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_246_btrfs_guard` | carte | `src/ameesh/resources.py` |
| unité 4150 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_2928_verdict_provenance` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_3236_brief_fenetre` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3396_housekeeping_unreadable` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3408_watch_foreign_pin` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_3480_ephemeres_hors_prise` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3483_train_canon_auto` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3532_fire_requeue_takeout` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_743_exec_only_serve` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/config.py`, `src/ameesh/mesh_cli.py` |
| unité 4150 | `tests.test_migrations_vues` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1467_train_runtime` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1680_packet_reservation` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2552_revue_source` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2556_tmp_copies_once` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3104_verdict_receipt` | carte | `src/ameesh/config.py`, `src/ameesh/mesh_cli.py` |
| unité 4150 | `tests.test_3115_gate_hold_lease_ttl` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l232b_migration_worker` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l317b_module_audit` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2954_incident_unique_ref` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2971_freeze_note_marks` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3416_review_correction_preview` | carte | `src/ameesh/config.py`, `src/ameesh/mesh_cli.py` |
| unité 4150 | `tests.test_3503_jit` | carte | `src/ameesh/cli.py`, `src/ameesh/config.py`, `src/ameesh/resources.py` |
| unité 4150 | `tests.test_l232e_host_lifecycle` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1647_liste_lots_journal` | carte | `src/ameesh/config.py`, `src/ameesh/mesh_cli.py` |
| unité 4150 | `tests.test_2584_backlog_panier` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l160g_migration_reservations` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l513_deepseek_offpeak` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l744_ci_entrypoint` | carte | `src/ameesh/config.py`, `src/ameesh/mesh_cli.py` |
| unité 4150 | `tests.test_2003_reap_volumes` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_288_cost_per_message` | carte | `src/ameesh/config.py`, `src/ameesh/mesh_cli.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_3123_volatile_hold` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3578_verdict_family_receipt` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1053_famille_au_gel` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1901_gate_unreadable` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2096_runner_memoire` | carte | `src/ameesh/config.py`, `src/ameesh/resources.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_1398_peek_boundaries` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_1580_marge_forfait` | carte | `src/ameesh/config.py`, `src/ameesh/mesh_cli.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_3056_panier_3038` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_653_db_connections` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_l232d_login_e2e` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1127_config_explicite` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_1883_overage_boucle` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3366_train_canon_requalify` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l83_chat_api` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1022_creneau_landed` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_3565_hold_race` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_802_canon_sync_concurrency` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3035_host_reconcile` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_l66_desktop` | carte | `src/ameesh/cli.py`, `src/ameesh/config.py`, `src/ameesh/resources.py` |
| unité 4150 | `tests.test_cost` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l539_review_lot` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_2211_roadmap_followups` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l232e_drain_handoffs` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_mail` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3335_corrector_survival` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_3632_merge_observed_vehicle` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1465_provenance` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_2130_d_projection` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_l31_visibility` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1467_train_cli` | carte | `src/ameesh/config.py` |
| unité 4150 | `tests.test_1467_train_events` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_1580_widget_forfait` | carte | `src/ameesh/config.py`, `src/ameesh/runner.py` |
| unité 4150 | `tests.test_1272_rejeu_seul_affiche` | carte | `src/ameesh/runner.py` |
| unité 4150 | `tests.test_1467_train_service` | carte | `src/ameesh/runner.py` |
| unité 4150 | `tests.test_2389_fire_no_land_once` | carte | `src/ameesh/runner.py` |
| unité 4150 | `tests.test_3273_idle_prompt_pilot` | carte | `src/ameesh/runner.py` |
| unité 4150 | `tests.test_3308_gh_base_ref` | carte | `src/ameesh/mesh_cli.py` |
| unité 4150 | `tests.test_706_rotation_latency` | carte | `src/ameesh/runner.py` |
| unité 4150 | `tests.test_1266_unreadable_turn_window` | carte | `src/ameesh/runner.py` |
| unité 4150 | `tests.test_1681_incident` | carte | `src/ameesh/runner.py` |
| unité 4150 | `tests.test_path_b_assembly` | carte | `src/ameesh/runner.py` |
| unité 4150 | `tests.test_l161g_ci_gel` | carte | `src/ameesh/mesh_cli.py` |
| unité 4150 | `tests.test_l234_auditor` | carte | `src/ameesh/runner.py` |
| unité 4150 | `tests.test_l148_stage1a2_alias` | carte | `src/ameesh/mesh_cli.py` |
| unité 4150 | `tests.test_l53_session_backup` | carte | `src/ameesh/runner.py` |
| unité 4150 | `tests.test_l77_resident_queue` | carte | `src/ameesh/runner.py` |
| unité 4150 | `tests.test_3307_basket_3300` | carte | `src/ameesh/runner.py` |
| unité 4172 | `tests.test_1179_sonde_pgbouncer` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_2022_disk_early_warning` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3402_alerts_advisory` | carte | `src/ameesh/notify.py`, `src/ameesh/role_timers.py` |
| unité 4172 | `tests.test_l50_group_orphan_work_items` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_2022_fleet` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_2602_event_dedupe` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3383_alerts_role_timer` | carte | `src/ameesh/notify.py`, `src/ameesh/role_timers.py` |
| unité 4172 | `tests.test_532_pgbouncer_pool` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_l161g_ci_gel` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l148_stage1a2_alias` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_2580_partage_comptes` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_models_entrypoint` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_1770_prevalidation_cli` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l132_alert_briefs` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_2951_github_observation` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3014_role_run` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3057_panier_vivacite` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_246_btrfs_guard` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3236_brief_fenetre` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3480_ephemeres_hors_prise` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3104_verdict_receipt` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l317b_module_audit` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3416_review_correction_preview` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l744_ci_entrypoint` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_288_cost_per_message` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_1901_gate_unreadable` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_653_db_connections` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_1883_overage_boucle` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3565_hold_race` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3632_merge_observed_vehicle` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_2931_packet_target` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_3447_prise_externe` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_1400_declaration_fusion` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_3575_alerts_basket` | carte | `src/ameesh/notify.py`, `src/ameesh/role_timers.py` |
| unité 4172 | `tests.test_l31_orphans` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3036_alert_liveness` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_l120_harness_versions` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l71_comptabilite` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_2845_github_watch` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_2354_swap_sans_psi` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l253_disk_volumes` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_3007_notify_fallback` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_l95_budget_juste` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_3512_slo_merged` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_l64_unmeasured` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/notify.py` |
| unité 4172 | `tests.test_1391_backlog_route` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_2200_pause_visible` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_3373_hold_lookalike` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l159d_host_measure` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_work` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l31_resources` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3288_fixes_owed` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l317_project_modules` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l790_outdated_restart` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_l160b_radar` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l233b_budget_handoff` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_l165_ep2_election_score` | carte | `src/ameesh/notify.py`, `src/ameesh/role_timers.py` |
| unité 4172 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l87c_alert_ack` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3337_presence_hors_executeur` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l317c_deep_analysis` | carte | `src/ameesh/mesh_cli.py` |
| unité 4172 | `tests.test_l46_corrections` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/notify.py` |
| unité 4172 | `tests.test_version` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_3619_host_crypto` | carte | `src/ameesh/notify.py`, `src/ameesh/role_timers.py` |
| unité 4172 | `tests.test_3345_profils_runner` | carte | `src/ameesh/notify.py` |
| unité 4172 | `tests.test_l480_chat_veille` | carte | `src/ameesh/notify.py` |
| unité 4173 | `tests.test_approve_equipes` | support | `tests/test_cluster.py` |
| unité 4173 | `tests.test_l56_role_executor` | support | `tests/test_cluster.py` |
| unité 4173 | `tests.test_1002_relance_perimee` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2873_freeze_gate` | carte | `src/ameesh/freeze_gate.py` |
| unité 4173 | `tests.test_3066_packet_required_checks` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_3308_gh_base_ref` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_1075_pace_multiplier` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_1561_fraicheur_releves` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_2022_fleet` | carte | `src/ameesh/mesh_cli.py` |
| unité 4173 | `tests.test_2233_underuse_limits` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_2648_next_choice_underuse` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_3437_debit_dependance` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_path_b_assembly` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4173 | `tests.test_313_comptes_deux_meshes` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_3643_host_critical_names` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_l160f_versions` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l161g_ci_gel` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2681_review_reading` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_catalog_store` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_l148_stage1a2_alias` | carte | `src/ameesh/mesh_cli.py` |
| unité 4173 | `tests.test_1467_train_storage` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_1672_placement_releve_perime` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_1770_prevalidation_store` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_2580_partage_comptes` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_2776_journal_designations` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_3185_relit_hors_equipe` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l79_chat_terminal` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_2759_gauge_history_isolation` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_2789_statut_libre` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_2908_train_packet_vehicle` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_2933_housekeeping_alert_identity` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l148_stage1a1_alias` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l148_stage3b_workstream_readers` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_l848_github_quota` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_models_entrypoint` | carte | `src/ameesh/mesh_cli.py` |
| unité 4173 | `tests.test_1227_suite_1176` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_1702_jauges_n` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_2879_alerts_disk_trend` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_3292_debit` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_3333_blocked_freeze_holds` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_3411_ephemeres_hors_designation` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l221_cout_result_sans_usage` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_l780_outdated_runner` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_2254_radar_passes` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_2339_autotake_migration` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_3293_cache_pressure` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l117_account_spread` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_1770_prevalidation_cli` | carte | `src/ameesh/mesh_cli.py` |
| unité 4173 | `tests.test_2812_superseded_corrector` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2877_stop_sweep_without_lock` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_2888_reviewer_fix_independence` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_3550_fire_queue_takeout_scope` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_508_runner_liveness` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l31_admission` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l480_chat_veille` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_1132_jauges_perimees` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2377_revue_famille` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_3180_host_cli_actor` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_803_classes_famille` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_bin_launchers` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_l132_alert_briefs` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_1467_train_fire` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_2951_github_observation` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_3014_role_run` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_3344_map_stale` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_472_zombie_turn_close` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_l142_env_isole` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2446_train_initial` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_2851_alerts_court` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_3057_panier_vivacite` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_3111_legacy_git_domain` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_3556_auto_take_holds` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_3592_floor_unreadable` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/mesh_cli.py`, `src/ameesh/storage/__init__.py` … |
| unité 4173 | `tests.test_2334_nexlink_paquet` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2928_verdict_provenance` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_3236_brief_fenetre` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_3396_housekeeping_unreadable` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_3408_watch_foreign_pin` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_3480_ephemeres_hors_prise` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_3532_fire_requeue_takeout` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_743_exec_only_serve` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/mesh_cli.py`, `src/ameesh/storage/__init__.py` … |
| unité 4173 | `tests.test_1467_train_runtime` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_1680_packet_reservation` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_2552_revue_source` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2556_tmp_copies_once` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_3104_verdict_receipt` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/mesh_cli.py`, `src/ameesh/storage/__init__.py` … |
| unité 4173 | `tests.test_3115_gate_hold_lease_ttl` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_l317b_module_audit` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_2954_incident_unique_ref` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_2971_freeze_note_marks` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_3416_review_correction_preview` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/mesh_cli.py`, `src/ameesh/storage/__init__.py` … |
| unité 4173 | `tests.test_3503_jit` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_l232e_host_lifecycle` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_l242c_critical_roles` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_1647_liste_lots_journal` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_2584_backlog_panier` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l160g_migration_reservations` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l513_deepseek_offpeak` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l744_ci_entrypoint` | carte | `src/ameesh/mesh_cli.py` |
| unité 4173 | `tests.test_2003_reap_volumes` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_288_cost_per_message` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/mesh_cli.py`, `src/ameesh/storage/__init__.py` … |
| unité 4173 | `tests.test_3123_volatile_hold` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_3578_verdict_family_receipt` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_1053_famille_au_gel` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_1901_gate_unreadable` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_2096_runner_memoire` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_1398_peek_boundaries` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_1580_marge_forfait` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_3056_panier_3038` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_653_db_connections` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_l232d_login_e2e` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_1127_config_explicite` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_1883_overage_boucle` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_3366_train_canon_requalify` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l83_chat_api` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_1022_creneau_landed` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_3565_hold_race` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_802_canon_sync_concurrency` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_3035_host_reconcile` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l66_desktop` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_1349_agent_peek` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_cost` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l539_review_lot` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2211_roadmap_followups` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_l232e_drain_handoffs` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_mail` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` |
| unité 4173 | `tests.test_3335_corrector_survival` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_3632_merge_observed_vehicle` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_1465_provenance` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2130_d_projection` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_l31_visibility` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_1467_train_cli` | carte | `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/__init__.py` … |
| unité 4173 | `tests.test_1467_train_events` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_1580_widget_forfait` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/storage/__init__.py`, `src/ameesh/storage/interface.py` … |
| unité 4173 | `tests.test_2931_packet_target` | carte | `src/ameesh/freeze_gate.py`, `src/ameesh/mesh_cli.py`, `src/ameesh/storage/__init__.py` … |
| unité 4173 | `tests.test_3209_skipped_by_construction` | carte | `src/ameesh/work.py` |
| unité 4173 | `tests.test_3221_nonconclusive_key` | carte | `src/ameesh/work.py` |
| unité 4173 | `tests.test_3530_ci_nonconclusive_dedup` | carte | `src/ameesh/work.py` |
| unité 4173 | `tests.test_1681_incident` | carte | `src/ameesh/work.py` |
| unité 4173 | `tests.test_2022_host_zombies` | carte | `src/ameesh/storage/interface.py` |
| unité 4173 | `tests.test_2256_fire_landing_path` | carte | `src/ameesh/work.py` |
| unité 4173 | `tests.test_3558_inherited_red` | carte | `src/ameesh/work.py` |
| unité 4173 | `tests.test_3307_basket_3300` | carte | `src/ameesh/freeze_gate.py` |
| unité 4173 | `tests.test_l159e6_guard_restart_alert` | carte | `src/ameesh/storage/interface.py` |
| unité 4219 | `tests.test_3527_release_identity` | carte | `src/ameesh/release_cutoff.py` |
| unité 4219 | `tests.test_2448_release_cutoff` | carte | `src/ameesh/release_cutoff.py` |
| unité 4219 | `tests.test_2601_packet_watch` | carte | `src/ameesh/release_cutoff.py` |
| unité 4219 | `tests.test_3209_skipped_by_construction` | carte | `src/ameesh/release_cutoff.py` |
| unité 4219 | `tests.test_3221_nonconclusive_key` | carte | `src/ameesh/release_cutoff.py` |
| unité 4219 | `tests.test_3530_ci_nonconclusive_dedup` | carte | `src/ameesh/release_cutoff.py` |
| unité 4219 | `tests.test_3535_flux_tolerance` | carte | `src/ameesh/release_cutoff.py` |
| unité 4219 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/release_cutoff.py` |
| unité 4219 | `tests.test_3529_nexlink_release` | carte | `src/ameesh/release_cutoff.py` |
| unité 4219 | `tests.test_3186_unconcluded_aggregate` | carte | `src/ameesh/release_cutoff.py` |
| unité 4219 | `tests.test_3291_staging_pending` | carte | `src/ameesh/release_cutoff.py` |
| unité 4219 | `tests.test_3244_release_optional_jobs` | carte | `src/ameesh/release_cutoff.py` |
| unité 4222 | `tests.test_1272_rejeu_seul_affiche` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_3273_idle_prompt_pilot` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_706_rotation_latency` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1466_relecture_config` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1681_incident` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l234_auditor` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l53_session_backup` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l77_resident_queue` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1672_placement_releve_perime` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2933_housekeeping_alert_identity` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_3307_basket_3300` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l498_turn_cgroup` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_bin_launchers` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_472_zombie_turn_close` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_3408_watch_foreign_pin` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_288_cost_per_message` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2096_runner_memoire` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1580_marge_forfait` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1349_agent_peek` | carte | `src/ameesh/events.py`, `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1465_provenance` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1467_train_events` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1580_widget_forfait` | carte | `src/ameesh/account_turn.py`, `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l31_orphans` | carte | `src/ameesh/account_turn.py`, `src/ameesh/events.py`, `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l120_harness_versions` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l795_preemption_evenement` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l232b_bootstrap_installer` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2905_stale_spend_key` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2839_codex_account_gauges` | carte | `src/ameesh/account_turn.py`, `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1466_pause_longue` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_3373_hold_lookalike` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_501_unleased_registry` | carte | `src/ameesh/events.py`, `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l31_resources` | carte | `src/ameesh/events.py`, `src/ameesh/runner.py` |
| unité 4222 | `tests.test_3288_fixes_owed` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_3451_courrier_info` | carte | `src/ameesh/account_turn.py`, `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l790_outdated_restart` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l490_deepseek_cgroup` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2610_rotation_tmpdir` | carte | `src/ameesh/account_turn.py`, `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2820_events_rotation` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l165_ep2_election_score` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/account_turn.py`, `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l242b_critical_accounts` | carte | `src/ameesh/account_turn.py`, `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l317c_deep_analysis` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l84_serve` | carte | `src/ameesh/events.py`, `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l165_ep3_election_standby` | carte | `src/ameesh/account_turn.py`, `src/ameesh/events.py`, `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l242b2_budget` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1037_bascule_harnais` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2743_housekeeping_clones` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_pressure_orchestrator` | carte | `src/ameesh/account_turn.py`, `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2623_orphan_levee_dependance` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1467_train_service` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2212_fire_fresh_radar` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_2389_fire_no_land_once` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_1266_unreadable_turn_window` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_path_b_assembly` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_313_comptes_deux_meshes` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_501_native_host_gate` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l88_ui` | carte | `src/ameesh/events.py` |
| unité 4222 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/runner.py` |
| unité 4222 | `tests.test_l780_outdated_runner` | carte | `src/ameesh/runner.py` |
| unité 4234 | `tests.test_2180_housekeeping_discovery` | support | `tests/test_l73_housekeeping.py` |
| unité 4234 | `tests.test_2743_housekeeping_clones` | support | `tests/test_l73_housekeeping.py` |
| unité 4234 | `tests.test_2827_housekeeping_local_refs` | support | `tests/test_l73_housekeeping.py` |
| unité 4234 | `tests.test_2933_housekeeping_alert_identity` | support | `tests/test_l73_housekeeping.py` |
| unité 4234 | `tests.test_3396_housekeeping_unreadable` | support | `tests/test_l73_housekeeping.py` |
| unité 4234 | `tests.test_l162_tmpfs` | support | `tests/test_l73_housekeeping.py` |
| unité 4234 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_3293_cache_pressure` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_2556_tmp_copies_once` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_2003_reap_volumes` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_762_menage_dryrun_episode` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_500_eviction_irremovable` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l230_build_artifacts` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_530_housekeeping_followups` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l219_housekeeping_tmp` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_3277_tmp_over_quota` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_1190_dryrun_cross_cache_inode` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l411_docker_storage` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l159e6_guard_restart_alert` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_472_zombie_turn_close` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_3503_jit` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_2096_runner_memoire` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l248b_deps_packagers` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l66_desktop` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l31_orphans` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_3226_dsh_scopes` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_2354_swap_sans_psi` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l253_disk_volumes` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_2839_codex_account_gauges` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l64_unmeasured` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l159d_host_measure` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l31_resources` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l790_outdated_restart` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_2610_rotation_tmpdir` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_2820_events_rotation` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l233b_budget_handoff` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l87c_alert_ack` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l242b_critical_accounts` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l84_serve` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l94_underuse` | carte | `src/ameesh/housekeeping.py` |
| unité 4234 | `tests.test_l165_ep3_election_standby` | carte | `src/ameesh/housekeeping.py` |
| unité 4239 | `tests.test_3114_host_wide_drain` | support | `tests/test_503_agent_host.py` |
| unité 4242 | `tests.test_3835_prior_freeze_timeout` | support | `tests/test_3399_dangling_supersedes.py` |
| unité 4253 | `tests.test_3575_alerts_basket` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_l31_orphans` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_501_unleased_registry` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_l31_resources` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_l165_ep4_pilot_gesture` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_l165_ep2_election_score` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_l165_ep3_election_standby` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_l31_pressure` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_rename` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_l78_conversations` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_1462_operator_pause` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_3568_alerts_delegate` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_2814_pressure_cgroup_capacity` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_3379_argparse_help` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_2009_sigterm_demarrage` | carte | `src/ameesh/election.py` |
| unité 4253 | `tests.test_l31_relocation` | carte | `src/ameesh/election.py` |
| unité 4259 | `tests.test_process_registry` | carte | `src/ameesh/process_registry.py`, `src/ameesh/process_verifiers.py` |
| unité 4259 | `tests.test_process_verifiers` | carte | `src/ameesh/process_verifiers.py` |
| unité 4259 | `tests.test_process_verifiers_seam` | carte | `src/ameesh/process_verifiers.py` |
| unité 4259 | `tests.test_tube_ferme` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l860_github_read` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_version` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_process_card` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_models_entrypoint` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_bin_launchers` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_743_exec_only_serve` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_1645_github_identity` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l744_ci_entrypoint` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_288_cost_per_message` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_3035_host_reconcile` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_2211_roadmap_followups` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_2931_packet_target` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_3447_prise_externe` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l120_harness_versions` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l71_comptabilite` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l59_activity` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_2354_swap_sans_psi` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l253_disk_volumes` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l95_budget_juste` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l64_unmeasured` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_1391_backlog_route` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_2200_pause_visible` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_3373_hold_lookalike` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l159d_host_measure` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_work` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l31_resources` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_3288_fixes_owed` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l317_project_modules` | carte | `src/ameesh/main.py`, `src/ameesh/process_verifiers.py` |
| unité 4259 | `tests.test_l790_outdated_restart` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l165_ep4_pilot_gesture` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l160b_radar` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l165_ep2_election_score` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l87c_alert_ack` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_3337_presence_hors_executeur` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l317c_deep_analysis` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l46_corrections` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_l84_serve` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_3460_agent_retire` | carte | `src/ameesh/main.py` |
| unité 4259 | `tests.test_1037_bascule_harnais` | carte | `src/ameesh/main.py` |
| unité 4266 | `tests.test_435_agent_projects` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_l43_perimetre_hote` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_l42_plusieurs_canons` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_468_agent_naming` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_placement` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_bout_en_bout` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_2022_fleet` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_l161g_ci_gel` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_l148_stage1a2_alias` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_models_entrypoint` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_1770_prevalidation_cli` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_3104_verdict_receipt` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_l744_ci_entrypoint` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_288_cost_per_message` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_2931_packet_target` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_3447_prise_externe` | carte | `src/ameesh/canon_cli.py` |
| unité 4266 | `tests.test_1400_declaration_fusion` | carte | `src/ameesh/canon_cli.py` |
| unité 4294 | `tests.test_l232b_bootstrap` | support | `tests/test_3526_runtime_seal.py` |
| unité 4294 | `tests.test_l232b_entree_operateur` | support | `tests/test_3526_runtime_seal.py` |
| unité 4304 | `tests.test_4113_packet_epoch` | support | `tests/test_3807_paquet_auto.py` |
| unité 4304 | `tests.test_4152_paquet_test_logs` | support | `tests/test_3807_paquet_auto.py` |
| unité 4304 | `tests.test_l161g_ci_gel` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4304 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4304 | `tests.test_2644_prevalidation_calls` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4304 | `tests.test_2813_packet_auto_merge` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4304 | `tests.test_2962_freeze_unresolved` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4304 | `tests.test_2442_verdict_carry` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4304 | `tests.test_2487_designation_carry` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4304 | `tests.test_2542_freeze_checks` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4308 | `tests.test_tube_ferme` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l860_github_read` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_version` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_process_card` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_models_entrypoint` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_bin_launchers` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/main.py`, `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_743_exec_only_serve` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_1645_github_identity` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l744_ci_entrypoint` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_288_cost_per_message` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_3035_host_reconcile` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_2211_roadmap_followups` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_2931_packet_target` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_3447_prise_externe` | carte | `src/ameesh/main.py`, `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_l120_harness_versions` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l71_comptabilite` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l59_activity` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_2354_swap_sans_psi` | carte | `src/ameesh/main.py`, `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_l253_disk_volumes` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l95_budget_juste` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l64_unmeasured` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_1391_backlog_route` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_2200_pause_visible` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_3373_hold_lookalike` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l159d_host_measure` | carte | `src/ameesh/main.py`, `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_work` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l31_resources` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_3288_fixes_owed` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l317_project_modules` | carte | `src/ameesh/main.py`, `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_l790_outdated_restart` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l165_ep4_pilot_gesture` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l160b_radar` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l165_ep2_election_score` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/main.py`, `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_l87c_alert_ack` | carte | `src/ameesh/main.py`, `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_3337_presence_hors_executeur` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l317c_deep_analysis` | carte | `src/ameesh/main.py`, `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_l46_corrections` | carte | `src/ameesh/main.py`, `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_l84_serve` | carte | `src/ameesh/main.py`, `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_3460_agent_retire` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_1037_bascule_harnais` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_2954_work_set_type` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l126_issues` | carte | `src/ameesh/main.py` |
| unité 4308 | `tests.test_l159c_plafonds_memoire` | carte | `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_l317_proof_helper` | carte | `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_l317_snapshot_limits` | carte | `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_3185_relit_hors_equipe` | carte | `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_l780_outdated_runner` | carte | `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_l31_admission` | carte | `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_2951_github_observation` | carte | `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_3344_map_stale` | carte | `src/ameesh/modules/__init__.py` |
| unité 4308 | `tests.test_l51_persona` | carte | `src/ameesh/modules/__init__.py` |
| unité 4185 | `tests.test_mail` | support | `tests/test_1323_actif.py` |
| unité 4235 | `tests.test_2022_fleet` | carte | `src/ameesh/mesh_cli.py` |
| unité 4235 | `tests.test_3437_debit_dependance` | carte | `src/ameesh/debit.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_3498_hold_externe` | carte | `src/ameesh/debit.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l161g_ci_gel` | carte | `src/ameesh/mesh_cli.py` |
| unité 4235 | `tests.test_l148_stage1a2_alias` | carte | `src/ameesh/mesh_cli.py` |
| unité 4235 | `tests.test_models_entrypoint` | carte | `src/ameesh/mesh_cli.py` |
| unité 4235 | `tests.test_3292_debit` | carte | `src/ameesh/debit.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_1770_prevalidation_cli` | carte | `src/ameesh/mesh_cli.py` |
| unité 4235 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_3104_verdict_receipt` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l744_ci_entrypoint` | carte | `src/ameesh/mesh_cli.py` |
| unité 4235 | `tests.test_288_cost_per_message` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_2931_packet_target` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_3447_prise_externe` | carte | `src/ameesh/debit.py`, `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_1400_declaration_fusion` | carte | `src/ameesh/mesh_cli.py` |
| unité 4235 | `tests.test_l120_harness_versions` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l71_comptabilite` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_2354_swap_sans_psi` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l253_disk_volumes` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l95_budget_juste` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l64_unmeasured` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_1391_backlog_route` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_2200_pause_visible` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_3373_hold_lookalike` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l159d_host_measure` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_work` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l31_resources` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_3288_fixes_owed` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l317_project_modules` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l160b_radar` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_3337_presence_hors_executeur` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l317c_deep_analysis` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l46_corrections` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l84_serve` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_3460_agent_retire` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_1037_bascule_harnais` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_2954_work_set_type` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l126_issues` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_2786_freeze_head` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_3589_stacked_freeze` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_2426_packet_close` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_2481_multi_migration_reservations` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_1272_rejeu_seul_affiche` | carte | `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_3495_dead_mail` | carte | `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l160f_versions` | carte | `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_l234_auditor` | carte | `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_2681_review_reading` | carte | `src/ameesh/planned_waits.py` |
| unité 4235 | `tests.test_2776_journal_designations` | carte | `src/ameesh/planned_waits.py` |

Non concluants (canon §3 : ne retiennent ni paquet ni release) :

- NON CONCLUANT : unité 4231 — modules tests.test_4231_unready_holder, tests.test_1272_rejeu_seul_affiche, tests.test_1467_train_service, tests.test_2212_fire_fresh_radar, tests.test_2389_fire_no_land_once, tests.test_3273_idle_prompt_pilot, tests.test_706_rotation_latency, tests.test_1266_unreadable_turn_window, tests.test_1466_relecture_config, tests.test_1681_incident, tests.test_path_b_assembly, tests.test_313_comptes_deux_meshes, tests.test_501_native_host_gate, tests.test_l234_auditor, tests.test_l53_session_backup, tests.test_l77_resident_queue, tests.test_1672_placement_releve_perime, tests.test_2933_housekeeping_alert_identity, tests.test_3307_basket_3300, tests.test_2167_host_housekeeping, tests.test_l498_turn_cgroup, tests.test_l780_outdated_runner, tests.test_2339_autotake_migration, tests.test_2877_stop_sweep_without_lock, tests.test_508_runner_liveness, tests.test_bin_launchers, tests.test_472_zombie_turn_close, tests.test_1021_tenue_attribution, tests.test_2934_budget_pause_notes, tests.test_3408_watch_foreign_pin, tests.test_l242c_critical_roles, tests.test_288_cost_per_message, tests.test_2096_runner_memoire, tests.test_1425_stop_sweep_suites, tests.test_1580_marge_forfait, tests.test_653_db_connections, tests.test_2526_orphan_dependance, tests.test_2582_alerts_console_timeout, tests.test_l93_device_state, tests.test_3035_host_reconcile, tests.test_1349_agent_peek, tests.test_1465_provenance, tests.test_1467_train_events, tests.test_1580_widget_forfait, tests.test_l31_orphans, tests.test_3472_handoff_runner, tests.test_l120_harness_versions, tests.test_l795_preemption_evenement, tests.test_l232b_bootstrap_installer, tests.test_2905_stale_spend_key, tests.test_339_orphan_settle, tests.test_3007_notify_fallback, tests.test_2839_codex_account_gauges, tests.test_l804_prise_executeur, tests.test_1466_pause_longue, tests.test_3373_hold_lookalike, tests.test_753_stop_returns_lots, tests.test_501_unleased_registry, tests.test_l165_election, tests.test_l31_resources, tests.test_3288_fixes_owed, tests.test_3451_courrier_info, tests.test_l790_outdated_restart, tests.test_l490_deepseek_cgroup, tests.test_2610_rotation_tmpdir, tests.test_2820_events_rotation, tests.test_l165_ep2_election_score, tests.test_2689_reveil_statut, tests.test_l242b_critical_accounts, tests.test_l317c_deep_analysis, tests.test_l84_serve, tests.test_l165_ep3_election_standby — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-mfz_5_yq.log
- NON CONCLUANT : unité 4245 — modules tests.test_4113_packet_epoch, tests.test_4245_lignee_meme_hote, tests.test_1021_tenue_attribution, tests.test_1467_train_runtime, tests.test_3115_gate_hold_lease_ttl, tests.test_3123_volatile_hold, tests.test_1425_stop_sweep_suites, tests.test_3565_hold_race, tests.test_2180_housekeeping_discovery, tests.test_1391_backlog_route, tests.test_l165_ep4_pilot_gesture, tests.test_l165_ep2_election_score, tests.test_l46_corrections, tests.test_1467_train_history, tests.test_1193_suite_dependency, tests.test_1280_work_close_done, tests.test_l157_estimations, tests.test_l540_team_queue, tests.test_1677_prise_empilee, tests.test_l36_vivacite, tests.test_l233_decide_only, tests.test_1003_merge_pr, tests.test_1467_train, tests.test_2443_packet_members, tests.test_3066_packet_required_checks, tests.test_3308_gh_base_ref, tests.test_2256_fire_landing_path, tests.test_3558_inherited_red, tests.test_2776_journal_designations, tests.test_2908_train_packet_vehicle, tests.test_3085_nonconclusive_gates — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-gcles3_c.log
- NON CONCLUANT : unité 4271 — modules tests.test_2449_garde_mecanique, tests.test_4271_paquet_series, tests.test_2457_garde_non_python, tests.test_1272_rejeu_seul_affiche, tests.test_1681_incident, tests.test_1770_mechanics_prevalidation, tests.test_1672_placement_releve_perime, tests.test_3085_nonconclusive_gates, tests.test_bin_launchers, tests.test_472_zombie_turn_close, tests.test_3592_floor_unreadable, tests.test_2296_admission_pression_memoire, tests.test_1021_tenue_attribution, tests.test_2934_budget_pause_notes, tests.test_3408_watch_foreign_pin, tests.test_810_multi_project_consumers, tests.test_l1616_mechanics, tests.test_3416_review_correction_preview, tests.test_2584_backlog_panier, tests.test_288_cost_per_message, tests.test_l1757_paquets_horaires, tests.test_2096_runner_memoire, tests.test_1425_stop_sweep_suites, tests.test_1580_marge_forfait, tests.test_2526_orphan_dependance, tests.test_2582_alerts_console_timeout, tests.test_1467_train_policy, tests.test_1349_agent_peek, tests.test_1467_train_events, tests.test_1580_widget_forfait, tests.test_2931_packet_target, tests.test_3415_designation_dead_pause, tests.test_3447_prise_externe, tests.test_l31_orphans, tests.test_l120_harness_versions, tests.test_l71_comptabilite, tests.test_2354_swap_sans_psi, tests.test_2905_stale_spend_key, tests.test_l253_disk_volumes, tests.test_2446_train_assembly, tests.test_2864_proposition_familles, tests.test_l95_budget_juste, tests.test_2839_codex_account_gauges, tests.test_l64_unmeasured, tests.test_1466_pause_longue, tests.test_1391_backlog_route, tests.test_2200_pause_visible, tests.test_2638_reviewer_fix_family, tests.test_3373_hold_lookalike, tests.test_l159d_host_measure, tests.test_501_unleased_registry, tests.test_3527_release_identity, tests.test_work, tests.test_l31_resources, tests.test_2557_replay_dedup_durable, tests.test_3288_fixes_owed, tests.test_3451_courrier_info, tests.test_l317_project_modules, tests.test_3572_canon_lowering_panier, tests.test_l790_outdated_restart, tests.test_l160b_radar, tests.test_3186_unconcluded_aggregate — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-7e3lc7cm.log
- NON CONCLUANT : unité 4150 — modules tests.test_4150_host_idle, tests.test_l64_unmeasured, tests.test_l84_serve, tests.test_1349_agent_peek, tests.test_l85_events, tests.test_l87_serve_actions, tests.test_l88_ui, tests.test_1002_relance_perimee, tests.test_1466_config_profil, tests.test_2212_fire_fresh_radar, tests.test_3346_masquage_secrets_doctor, tests.test_1075_pace_multiplier, tests.test_1466_relecture_config, tests.test_1561_fraicheur_releves, tests.test_2022_fleet, tests.test_2022_host_zombies, tests.test_3437_debit_dependance, tests.test_3498_hold_externe, tests.test_313_comptes_deux_meshes, tests.test_3643_host_critical_names, tests.test_501_native_host_gate, tests.test_l107_executor_contract, tests.test_l160f_versions, tests.test_version, tests.test_2681_review_reading, tests.test_3619_host_crypto, tests.test_898_pause_rythme, tests.test_catalog_store, tests.test_l159i_vm_recipe, tests.test_1467_train_storage, tests.test_1672_placement_releve_perime, tests.test_1770_prevalidation_store, tests.test_2580_partage_comptes, tests.test_2776_journal_designations, tests.test_3185_relit_hors_equipe, tests.test_l148_stage3_workstream, tests.test_l79_chat_terminal, tests.test_2759_gauge_history_isolation, tests.test_2789_statut_libre, tests.test_2908_train_packet_vehicle, tests.test_2933_housekeeping_alert_identity, tests.test_l148_stage1a1_alias, tests.test_l148_stage3b_workstream_readers, tests.test_models_entrypoint, tests.test_1227_suite_1176, tests.test_1702_jauges_n, tests.test_2167_host_housekeeping, tests.test_2879_alerts_disk_trend, tests.test_3292_debit, tests.test_3333_blocked_freeze_holds, tests.test_3411_ephemeres_hors_designation, tests.test_l221_cout_result_sans_usage, tests.test_l780_outdated_runner, tests.test_2254_radar_passes, tests.test_2339_autotake_migration, tests.test_3293_cache_pressure, tests.test_3345_profils_runner, tests.test_l117_account_spread, tests.test_2812_superseded_corrector, tests.test_2877_stop_sweep_without_lock, tests.test_2888_reviewer_fix_independence, tests.test_3550_fire_queue_takeout_scope, tests.test_508_runner_liveness, tests.test_l31_admission, tests.test_l480_chat_veille, tests.test_1132_jauges_perimees, tests.test_2377_revue_famille, tests.test_3180_host_cli_actor, tests.test_803_classes_famille, tests.test_bin_launchers, tests.test_l132_alert_briefs, tests.test_1467_train_fire, tests.test_2951_github_observation, tests.test_3014_role_run, tests.test_3344_map_stale, tests.test_472_zombie_turn_close, tests.test_l142_env_isole, tests.test_l159e_guard, tests.test_2446_train_initial, tests.test_2851_alerts_court, tests.test_3057_panier_vivacite, tests.test_3111_legacy_git_domain, tests.test_3556_auto_take_holds, tests.test_3592_floor_unreadable, tests.test_2296_admission_pression_memoire, tests.test_2334_nexlink_paquet, tests.test_246_btrfs_guard, tests.test_1021_tenue_attribution, tests.test_2928_verdict_provenance, tests.test_2934_budget_pause_notes, tests.test_3236_brief_fenetre, tests.test_3396_housekeeping_unreadable, tests.test_3408_watch_foreign_pin, tests.test_3480_ephemeres_hors_prise, tests.test_3483_train_canon_auto, tests.test_3532_fire_requeue_takeout, tests.test_743_exec_only_serve, tests.test_810_multi_project_consumers, tests.test_migrations_vues, tests.test_1467_train_runtime, tests.test_1680_packet_reservation, tests.test_2552_revue_source, tests.test_2556_tmp_copies_once, tests.test_3104_verdict_receipt, tests.test_3115_gate_hold_lease_ttl, tests.test_l232b_migration_worker, tests.test_l317b_module_audit, tests.test_2954_incident_unique_ref, tests.test_2971_freeze_note_marks, tests.test_3416_review_correction_preview, tests.test_3503_jit, tests.test_l232e_host_lifecycle, tests.test_1647_liste_lots_journal, tests.test_2584_backlog_panier, tests.test_l160g_migration_reservations, tests.test_l513_deepseek_offpeak, tests.test_l744_ci_entrypoint, tests.test_2003_reap_volumes, tests.test_288_cost_per_message, tests.test_3123_volatile_hold, tests.test_3578_verdict_family_receipt, tests.test_1053_famille_au_gel, tests.test_1901_gate_unreadable, tests.test_2096_runner_memoire, tests.test_1398_peek_boundaries, tests.test_1425_stop_sweep_suites, tests.test_1580_marge_forfait, tests.test_3056_panier_3038, tests.test_653_db_connections, tests.test_l232d_login_e2e, tests.test_1127_config_explicite, tests.test_2526_orphan_dependance, tests.test_1883_overage_boucle, tests.test_3366_train_canon_requalify, tests.test_l83_chat_api, tests.test_1022_creneau_landed, tests.test_2582_alerts_console_timeout, tests.test_3565_hold_race, tests.test_802_canon_sync_concurrency, tests.test_3035_host_reconcile, tests.test_l66_desktop, tests.test_cost, tests.test_l539_review_lot, tests.test_2211_roadmap_followups, tests.test_l232e_drain_handoffs, tests.test_mail, tests.test_3335_corrector_survival, tests.test_3632_merge_observed_vehicle, tests.test_1465_provenance, tests.test_2130_d_projection, tests.test_l31_visibility, tests.test_1467_train_cli, tests.test_1467_train_events, tests.test_1580_widget_forfait, tests.test_1272_rejeu_seul_affiche, tests.test_1467_train_service, tests.test_2389_fire_no_land_once, tests.test_3273_idle_prompt_pilot, tests.test_3308_gh_base_ref, tests.test_706_rotation_latency, tests.test_1266_unreadable_turn_window, tests.test_1681_incident, tests.test_path_b_assembly, tests.test_l161g_ci_gel, tests.test_l234_auditor, tests.test_l148_stage1a2_alias, tests.test_l53_session_backup, tests.test_l77_resident_queue, tests.test_3307_basket_3300 — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-wlpr_d1l.log
- NON CONCLUANT : unité 4172 — modules tests.test_3327_role_timers, tests.test_4172_backlog_coherence, tests.test_1179_sonde_pgbouncer, tests.test_2022_disk_early_warning, tests.test_3402_alerts_advisory, tests.test_l50_group_orphan_work_items, tests.test_2022_fleet, tests.test_2602_event_dedupe, tests.test_3383_alerts_role_timer, tests.test_532_pgbouncer_pool, tests.test_l161g_ci_gel, tests.test_l148_stage1a2_alias, tests.test_2580_partage_comptes, tests.test_models_entrypoint, tests.test_1770_prevalidation_cli, tests.test_l132_alert_briefs, tests.test_2951_github_observation, tests.test_3014_role_run, tests.test_3057_panier_vivacite, tests.test_2296_admission_pression_memoire, tests.test_246_btrfs_guard, tests.test_1021_tenue_attribution, tests.test_3236_brief_fenetre, tests.test_3480_ephemeres_hors_prise, tests.test_810_multi_project_consumers, tests.test_3104_verdict_receipt, tests.test_l317b_module_audit, tests.test_3416_review_correction_preview, tests.test_l744_ci_entrypoint, tests.test_288_cost_per_message, tests.test_1901_gate_unreadable, tests.test_653_db_connections, tests.test_1883_overage_boucle, tests.test_3565_hold_race, tests.test_3632_merge_observed_vehicle, tests.test_2931_packet_target, tests.test_3447_prise_externe, tests.test_1400_declaration_fusion, tests.test_3575_alerts_basket, tests.test_l31_orphans, tests.test_3036_alert_liveness, tests.test_l120_harness_versions, tests.test_l71_comptabilite, tests.test_2845_github_watch, tests.test_2354_swap_sans_psi, tests.test_l253_disk_volumes, tests.test_3007_notify_fallback, tests.test_l95_budget_juste, tests.test_3512_slo_merged, tests.test_l64_unmeasured, tests.test_1391_backlog_route, tests.test_2200_pause_visible, tests.test_3373_hold_lookalike, tests.test_l159d_host_measure, tests.test_work, tests.test_l31_resources, tests.test_3288_fixes_owed, tests.test_l317_project_modules, tests.test_l790_outdated_restart, tests.test_l160b_radar, tests.test_l233b_budget_handoff, tests.test_l165_ep2_election_score, tests.test_2689_reveil_statut, tests.test_l87c_alert_ack, tests.test_3337_presence_hors_executeur, tests.test_l317c_deep_analysis, tests.test_l46_corrections, tests.test_version, tests.test_3619_host_crypto, tests.test_3345_profils_runner, tests.test_l480_chat_veille — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-ogzcreqf.log
- NON CONCLUANT : unité 4173 — modules tests.test_4173_empreinte, tests.test_cluster, tests.test_approve_equipes, tests.test_l56_role_executor, tests.test_1002_relance_perimee, tests.test_2873_freeze_gate, tests.test_3066_packet_required_checks, tests.test_3308_gh_base_ref, tests.test_1075_pace_multiplier, tests.test_1561_fraicheur_releves, tests.test_2022_fleet, tests.test_2233_underuse_limits, tests.test_2648_next_choice_underuse, tests.test_3437_debit_dependance, tests.test_path_b_assembly, tests.test_313_comptes_deux_meshes, tests.test_3643_host_critical_names, tests.test_l160f_versions, tests.test_l161g_ci_gel, tests.test_2681_review_reading, tests.test_catalog_store, tests.test_l148_stage1a2_alias, tests.test_1467_train_storage, tests.test_1672_placement_releve_perime, tests.test_1770_prevalidation_store, tests.test_2580_partage_comptes, tests.test_2776_journal_designations, tests.test_3185_relit_hors_equipe, tests.test_l79_chat_terminal, tests.test_2759_gauge_history_isolation, tests.test_2789_statut_libre, tests.test_2908_train_packet_vehicle, tests.test_2933_housekeeping_alert_identity, tests.test_3085_nonconclusive_gates, tests.test_l148_stage1a1_alias, tests.test_l148_stage3b_workstream_readers, tests.test_l848_github_quota, tests.test_models_entrypoint, tests.test_1227_suite_1176, tests.test_1702_jauges_n, tests.test_2167_host_housekeeping, tests.test_2879_alerts_disk_trend, tests.test_3292_debit, tests.test_3333_blocked_freeze_holds, tests.test_3411_ephemeres_hors_designation, tests.test_l221_cout_result_sans_usage, tests.test_l780_outdated_runner, tests.test_2254_radar_passes, tests.test_2339_autotake_migration, tests.test_3293_cache_pressure, tests.test_l117_account_spread, tests.test_1770_prevalidation_cli, tests.test_2812_superseded_corrector, tests.test_2877_stop_sweep_without_lock, tests.test_2888_reviewer_fix_independence, tests.test_3550_fire_queue_takeout_scope, tests.test_508_runner_liveness, tests.test_l31_admission, tests.test_l480_chat_veille, tests.test_1132_jauges_perimees, tests.test_2377_revue_famille, tests.test_3180_host_cli_actor, tests.test_803_classes_famille, tests.test_bin_launchers, tests.test_l132_alert_briefs, tests.test_1467_train_fire, tests.test_2951_github_observation, tests.test_3014_role_run, tests.test_3344_map_stale, tests.test_472_zombie_turn_close, tests.test_l142_env_isole, tests.test_2446_train_initial, tests.test_2851_alerts_court, tests.test_3057_panier_vivacite, tests.test_3111_legacy_git_domain, tests.test_3556_auto_take_holds, tests.test_3592_floor_unreadable, tests.test_2296_admission_pression_memoire, tests.test_2334_nexlink_paquet, tests.test_1021_tenue_attribution, tests.test_2928_verdict_provenance, tests.test_2934_budget_pause_notes, tests.test_3236_brief_fenetre, tests.test_3396_housekeeping_unreadable, tests.test_3408_watch_foreign_pin, tests.test_3480_ephemeres_hors_prise, tests.test_3532_fire_requeue_takeout, tests.test_743_exec_only_serve, tests.test_810_multi_project_consumers, tests.test_1467_train_runtime, tests.test_1680_packet_reservation, tests.test_2552_revue_source, tests.test_2556_tmp_copies_once, tests.test_3104_verdict_receipt, tests.test_3115_gate_hold_lease_ttl, tests.test_l317b_module_audit, tests.test_2954_incident_unique_ref, tests.test_2971_freeze_note_marks, tests.test_3416_review_correction_preview, tests.test_3503_jit, tests.test_l232e_host_lifecycle, tests.test_l242c_critical_roles, tests.test_1647_liste_lots_journal, tests.test_2584_backlog_panier, tests.test_l160g_migration_reservations, tests.test_l513_deepseek_offpeak, tests.test_l744_ci_entrypoint, tests.test_2003_reap_volumes, tests.test_288_cost_per_message, tests.test_3123_volatile_hold, tests.test_3578_verdict_family_receipt, tests.test_1053_famille_au_gel, tests.test_1901_gate_unreadable, tests.test_2096_runner_memoire, tests.test_1398_peek_boundaries, tests.test_1425_stop_sweep_suites, tests.test_1580_marge_forfait, tests.test_3056_panier_3038, tests.test_653_db_connections, tests.test_l232d_login_e2e, tests.test_1127_config_explicite, tests.test_2526_orphan_dependance, tests.test_1883_overage_boucle, tests.test_3366_train_canon_requalify, tests.test_l83_chat_api, tests.test_1022_creneau_landed, tests.test_2582_alerts_console_timeout, tests.test_3565_hold_race, tests.test_802_canon_sync_concurrency, tests.test_3035_host_reconcile, tests.test_l66_desktop, tests.test_1349_agent_peek, tests.test_cost, tests.test_l539_review_lot, tests.test_2211_roadmap_followups, tests.test_l232e_drain_handoffs, tests.test_mail, tests.test_3335_corrector_survival, tests.test_3632_merge_observed_vehicle, tests.test_1465_provenance, tests.test_2130_d_projection, tests.test_l31_visibility, tests.test_1467_train_cli, tests.test_1467_train_events, tests.test_1580_widget_forfait, tests.test_2931_packet_target, tests.test_3209_skipped_by_construction, tests.test_3221_nonconclusive_key, tests.test_3530_ci_nonconclusive_dedup, tests.test_1681_incident, tests.test_2022_host_zombies, tests.test_2256_fire_landing_path, tests.test_3558_inherited_red, tests.test_3307_basket_3300, tests.test_l159e6_guard_restart_alert — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-dg5wl75h.log
- NON CONCLUANT : unité 4222 — modules tests.test_3218_grant_latency, tests.test_1272_rejeu_seul_affiche, tests.test_3273_idle_prompt_pilot, tests.test_706_rotation_latency, tests.test_1466_relecture_config, tests.test_1681_incident, tests.test_l234_auditor, tests.test_l53_session_backup, tests.test_l77_resident_queue, tests.test_1672_placement_releve_perime, tests.test_2933_housekeeping_alert_identity, tests.test_3307_basket_3300, tests.test_l498_turn_cgroup, tests.test_bin_launchers, tests.test_472_zombie_turn_close, tests.test_1021_tenue_attribution, tests.test_2934_budget_pause_notes, tests.test_3408_watch_foreign_pin, tests.test_288_cost_per_message, tests.test_2096_runner_memoire, tests.test_1425_stop_sweep_suites, tests.test_1580_marge_forfait, tests.test_2526_orphan_dependance, tests.test_2582_alerts_console_timeout, tests.test_1349_agent_peek, tests.test_1465_provenance, tests.test_1467_train_events, tests.test_1580_widget_forfait, tests.test_l31_orphans, tests.test_l120_harness_versions, tests.test_l795_preemption_evenement, tests.test_l232b_bootstrap_installer, tests.test_2905_stale_spend_key, tests.test_2839_codex_account_gauges, tests.test_1466_pause_longue, tests.test_3373_hold_lookalike, tests.test_501_unleased_registry, tests.test_l31_resources, tests.test_3288_fixes_owed, tests.test_3451_courrier_info, tests.test_l790_outdated_restart, tests.test_l490_deepseek_cgroup, tests.test_2610_rotation_tmpdir, tests.test_2820_events_rotation, tests.test_l165_ep2_election_score, tests.test_2689_reveil_statut, tests.test_l242b_critical_accounts, tests.test_l317c_deep_analysis, tests.test_l84_serve, tests.test_l165_ep3_election_standby, tests.test_l242b2_budget, tests.test_1037_bascule_harnais, tests.test_2743_housekeeping_clones, tests.test_pressure_orchestrator, tests.test_2623_orphan_levee_dependance, tests.test_1467_train_service, tests.test_2212_fire_fresh_radar, tests.test_2389_fire_no_land_once, tests.test_1266_unreadable_turn_window, tests.test_path_b_assembly, tests.test_313_comptes_deux_meshes, tests.test_501_native_host_gate, tests.test_l88_ui, tests.test_2167_host_housekeeping, tests.test_l780_outdated_runner — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-n1si8idm.log
- NON CONCLUANT : unité 4234 — modules tests.test_3802_host_pass_lock, tests.test_4234_housekeeping_muet, tests.test_l73_housekeeping, tests.test_2180_housekeeping_discovery, tests.test_2743_housekeeping_clones, tests.test_2827_housekeeping_local_refs, tests.test_2933_housekeeping_alert_identity, tests.test_3396_housekeeping_unreadable, tests.test_l162_tmpfs, tests.test_2167_host_housekeeping, tests.test_3293_cache_pressure, tests.test_2556_tmp_copies_once, tests.test_2003_reap_volumes, tests.test_762_menage_dryrun_episode, tests.test_500_eviction_irremovable, tests.test_l230_build_artifacts, tests.test_530_housekeeping_followups, tests.test_l219_housekeeping_tmp, tests.test_3277_tmp_over_quota, tests.test_1190_dryrun_cross_cache_inode, tests.test_l411_docker_storage, tests.test_l159e6_guard_restart_alert, tests.test_472_zombie_turn_close, tests.test_2296_admission_pression_memoire, tests.test_3503_jit, tests.test_2096_runner_memoire, tests.test_l248b_deps_packagers, tests.test_l66_desktop, tests.test_l31_orphans, tests.test_3226_dsh_scopes, tests.test_2354_swap_sans_psi, tests.test_l253_disk_volumes, tests.test_2839_codex_account_gauges, tests.test_l64_unmeasured, tests.test_l159d_host_measure, tests.test_l31_resources, tests.test_l790_outdated_restart, tests.test_2610_rotation_tmpdir, tests.test_2820_events_rotation, tests.test_l233b_budget_handoff, tests.test_l87c_alert_ack, tests.test_l242b_critical_accounts, tests.test_l84_serve, tests.test_l94_underuse, tests.test_l165_ep3_election_standby — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-wvk8mrl3.log
- NON CONCLUANT : unité 4239 — modules tests.test_503_agent_host, tests.test_3114_host_wide_drain — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-01l8_tzu.log
- NON CONCLUANT : unité 4253 — modules tests.test_4253_reservation_reprise_longue, tests.test_3575_alerts_basket, tests.test_l31_orphans, tests.test_501_unleased_registry, tests.test_l31_resources, tests.test_l165_ep4_pilot_gesture, tests.test_l165_ep2_election_score, tests.test_l165_ep3_election_standby, tests.test_l31_pressure, tests.test_rename, tests.test_l78_conversations, tests.test_1462_operator_pause, tests.test_3568_alerts_delegate, tests.test_2814_pressure_cgroup_capacity, tests.test_3379_argparse_help, tests.test_2009_sigterm_demarrage, tests.test_l31_relocation — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-9gsasne4.log
- NON CONCLUANT : unité 4259 — modules tests.test_4007_revision_transfer, tests.test_4134_process_admission, tests.test_4259_client_literals, tests.test_process_approvals, tests.test_process_approvals_704, tests.test_process_registry, tests.test_process_verifiers, tests.test_process_verifiers_seam, tests.test_tube_ferme, tests.test_l860_github_read, tests.test_version, tests.test_process_card, tests.test_models_entrypoint, tests.test_bin_launchers, tests.test_2296_admission_pression_memoire, tests.test_743_exec_only_serve, tests.test_810_multi_project_consumers, tests.test_1645_github_identity, tests.test_l744_ci_entrypoint, tests.test_288_cost_per_message, tests.test_3035_host_reconcile, tests.test_2211_roadmap_followups, tests.test_2931_packet_target, tests.test_3447_prise_externe, tests.test_l120_harness_versions, tests.test_l71_comptabilite, tests.test_l59_activity, tests.test_2354_swap_sans_psi, tests.test_l253_disk_volumes, tests.test_l95_budget_juste, tests.test_l64_unmeasured, tests.test_1391_backlog_route, tests.test_2200_pause_visible, tests.test_3373_hold_lookalike, tests.test_l159d_host_measure, tests.test_work, tests.test_l31_resources, tests.test_3288_fixes_owed, tests.test_l317_project_modules, tests.test_l790_outdated_restart, tests.test_l165_ep4_pilot_gesture, tests.test_l160b_radar, tests.test_l165_ep2_election_score, tests.test_2689_reveil_statut, tests.test_l87c_alert_ack, tests.test_3337_presence_hors_executeur, tests.test_l317c_deep_analysis, tests.test_l46_corrections, tests.test_l84_serve, tests.test_3460_agent_retire, tests.test_1037_bascule_harnais — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-5xaqwbzk.log
- NON CONCLUANT : unité 4266 — modules tests.test_l4264_canon_check_registre, tests.test_435_agent_projects, tests.test_l43_perimetre_hote, tests.test_l42_plusieurs_canons, tests.test_468_agent_naming, tests.test_placement, tests.test_bout_en_bout, tests.test_2022_fleet, tests.test_l161g_ci_gel, tests.test_l148_stage1a2_alias, tests.test_models_entrypoint, tests.test_1770_prevalidation_cli, tests.test_2296_admission_pression_memoire, tests.test_810_multi_project_consumers, tests.test_3104_verdict_receipt, tests.test_l744_ci_entrypoint, tests.test_288_cost_per_message, tests.test_2931_packet_target, tests.test_3447_prise_externe, tests.test_1400_declaration_fusion — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-4i108moy.log
- NON CONCLUANT : unité 4304 — modules tests.test_3807_paquet_auto, tests.test_3931_paquet_rouge_cible, tests.test_4304_paquet_poussee_precoce, tests.test_4113_packet_epoch, tests.test_4152_paquet_test_logs, tests.test_l161g_ci_gel, tests.test_3085_nonconclusive_gates, tests.test_2644_prevalidation_calls, tests.test_2813_packet_auto_merge, tests.test_2962_freeze_unresolved, tests.test_2442_verdict_carry, tests.test_2487_designation_carry, tests.test_2542_freeze_checks — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-8s3j09pq.log
- NON CONCLUANT : unité 4308 — modules tests.test_generic_core, tests.test_l98_modules_registry, tests.test_tube_ferme, tests.test_l860_github_read, tests.test_version, tests.test_process_card, tests.test_models_entrypoint, tests.test_bin_launchers, tests.test_2296_admission_pression_memoire, tests.test_743_exec_only_serve, tests.test_810_multi_project_consumers, tests.test_1645_github_identity, tests.test_l744_ci_entrypoint, tests.test_288_cost_per_message, tests.test_3035_host_reconcile, tests.test_2211_roadmap_followups, tests.test_2931_packet_target, tests.test_3447_prise_externe, tests.test_l120_harness_versions, tests.test_l71_comptabilite, tests.test_l59_activity, tests.test_2354_swap_sans_psi, tests.test_l253_disk_volumes, tests.test_l95_budget_juste, tests.test_l64_unmeasured, tests.test_1391_backlog_route, tests.test_2200_pause_visible, tests.test_3373_hold_lookalike, tests.test_l159d_host_measure, tests.test_work, tests.test_l31_resources, tests.test_3288_fixes_owed, tests.test_l317_project_modules, tests.test_l790_outdated_restart, tests.test_l165_ep4_pilot_gesture, tests.test_l160b_radar, tests.test_l165_ep2_election_score, tests.test_2689_reveil_statut, tests.test_l87c_alert_ack, tests.test_3337_presence_hors_executeur, tests.test_l317c_deep_analysis, tests.test_l46_corrections, tests.test_l84_serve, tests.test_3460_agent_retire, tests.test_1037_bascule_harnais, tests.test_2954_work_set_type, tests.test_l126_issues, tests.test_l159c_plafonds_memoire, tests.test_l317_proof_helper, tests.test_l317_snapshot_limits, tests.test_3185_relit_hors_equipe, tests.test_l780_outdated_runner, tests.test_l31_admission, tests.test_2951_github_observation, tests.test_3344_map_stale, tests.test_l51_persona — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-p99x37p2.log
- NON CONCLUANT : unité 4235 — modules tests.test_4235_hold_fenetre, tests.test_2022_fleet, tests.test_3437_debit_dependance, tests.test_3498_hold_externe, tests.test_l161g_ci_gel, tests.test_l148_stage1a2_alias, tests.test_models_entrypoint, tests.test_3292_debit, tests.test_1770_prevalidation_cli, tests.test_2296_admission_pression_memoire, tests.test_810_multi_project_consumers, tests.test_3104_verdict_receipt, tests.test_l744_ci_entrypoint, tests.test_288_cost_per_message, tests.test_2931_packet_target, tests.test_3447_prise_externe, tests.test_1400_declaration_fusion, tests.test_l120_harness_versions, tests.test_l71_comptabilite, tests.test_2354_swap_sans_psi, tests.test_l253_disk_volumes, tests.test_l95_budget_juste, tests.test_l64_unmeasured, tests.test_1391_backlog_route, tests.test_2200_pause_visible, tests.test_3373_hold_lookalike, tests.test_l159d_host_measure, tests.test_work, tests.test_l31_resources, tests.test_3288_fixes_owed, tests.test_l317_project_modules, tests.test_l160b_radar, tests.test_2689_reveil_statut, tests.test_3337_presence_hors_executeur, tests.test_l317c_deep_analysis, tests.test_l46_corrections, tests.test_l84_serve, tests.test_3460_agent_retire, tests.test_1037_bascule_harnais, tests.test_2954_work_set_type, tests.test_l126_issues, tests.test_2786_freeze_head, tests.test_3589_stacked_freeze, tests.test_2426_packet_close, tests.test_2481_multi_migration_reservations, tests.test_1272_rejeu_seul_affiche, tests.test_3495_dead_mail, tests.test_l160f_versions, tests.test_l234_auditor, tests.test_2681_review_reading, tests.test_2776_journal_designations — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-9qmlp916.log
- NON CONCLUANT : contrats — modules tests.test_2340_auditeur_role, tests.test_2449_garde_mecanique, tests.test_2457_garde_non_python, tests.test_339_orphan_settle, tests.test_cluster, tests.test_events_attach, tests.test_l31_orphans, tests.test_l50_group_orphan_work_items, tests.test_l72_panne_de_base, tests.test_migrations, tests.test_3752_gabarit, tests.test_l161f_plan — délai dépassé — extrait : commande /home/ameesh/.local/share/ameesh/venv-03dab6e/bin/python : délai de 90 s dépassé — journal local : /home/ameesh/.local/state/ameesh/roles/mesh-design/test-logs/series-6_1_jm80.log

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
