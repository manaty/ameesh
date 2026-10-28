# Paquet integration/paquet-h-20261027-46 — composition (§12), sur main `80c403b7a560`

Base : `main` **`80c403b7a560266cd4b8710c97e6c2769ce81a21`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261027-46`, cible `main`.
Porteur : **#4085**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (8 unités, 8 lots)

| lot | source composée |
|---|---|
| #3981 | `e7d4937c81b22aae55873433c5a68fa1858beac0` |
| #4071 | `1b786270a73b836f7b55d3342e2e50f73784c8d1` |
| #4075 | `b7039ff31ee74e2309f7d283d004c345d4316bb8` |
| #4082 | `a729100b4b85ad11609f3b471cc575a0eacf48d8` |
| #4084 | `f97d73b0e55b8f4f1649a85ab59063edc036adaf` |
| #3519 | `83de9ae528ebe32cd245c9e8fe742a6c875a62a3` |
| #4046 | `7cf36d1a5511d4becaa27cde3be0f8c8d1e8ff39` |
| #4073 | `dc09297ec0f85f363f55659a179bb1876d8b4c4d` |

## Composition (8 unités, 8 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3981 | `e7d4937c81b2` | `5f48adf2217f` | composition |
| 2 | #4071 | `1b786270a73b` | `288adb7b7f29` | composition |
| 3 | #4075 | `b7039ff31ee7` | `c6a0ce51afe6` | composition |
| 4 | #4082 | `a729100b4b85` | `f18582bf59fe` | composition |
| 5 | #4084 | `f97d73b0e55b` | `6c53ca375155` | composition |
| 6 | #3519 | `83de9ae528eb` | `1af980db1595` | composition |
| 7 | #4046 | `7cf36d1a5511` | `048016aaffe1` | composition |
| 8 | #4073 | `dc09297ec0f8` | `8fc3e9740280` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 8 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 18.3 s.
- Tests locaux : 9 séries, 206 modules, 5088 tests exécutés, 0 échec(s), 0 run(s) rouge(s) sans effet, 0 non concluant(s), 12 sauté(s) : `tests.test_1002_relance_perimee`, `tests.test_1021_tenue_attribution`, `tests.test_1022_creneau_landed`, `tests.test_1053_famille_au_gel`, `tests.test_1075_pace_multiplier`, `tests.test_1127_config_explicite`, `tests.test_1155_staging`, `tests.test_1193_suite_dependency`, `tests.test_1227_suite_1176`, `tests.test_1272_rejeu_seul_affiche`, `tests.test_1280_work_close_done`, `tests.test_1323_actif`, `tests.test_1391_backlog_route`, `tests.test_1400_declaration_fusion`, `tests.test_1425_stop_sweep_suites`, `tests.test_1466_pause_longue`, `tests.test_1467_train`, `tests.test_1467_train_cli`, `tests.test_1467_train_events`, `tests.test_1467_train_fire`, `tests.test_1467_train_runtime`, `tests.test_1467_train_storage`, `tests.test_1467_train_transports`, `tests.test_1580_widget_forfait`, `tests.test_1647_liste_lots_journal`, `tests.test_1651_ci_parts`, `tests.test_1656_auto_merge`, `tests.test_1677_prise_empilee`, `tests.test_1680_packet_reservation`, `tests.test_1682_auto_merge_constat`, `tests.test_1718_land_eject_unreadable`, `tests.test_1770_mechanics_prevalidation`, `tests.test_1770_prevalidation`, `tests.test_1770_prevalidation_cli`, `tests.test_1770_prevalidation_store`, `tests.test_1819_no_replace_objects`, `tests.test_1820_plan_df_conflict`, `tests.test_1901_gate_unreadable`, `tests.test_2022_fleet`, `tests.test_2102_detacher_branche`, `tests.test_2130_d_projection`, `tests.test_2167_host_housekeeping`, `tests.test_2200_pause_visible`, `tests.test_2211_roadmap_followups`, `tests.test_2254_radar_passes`, `tests.test_2286_queue_required_checks`, `tests.test_2296_admission_pression_memoire`, `tests.test_2328_ci_target_base`, `tests.test_2334_nexlink_paquet`, `tests.test_2339_autotake_migration`, `tests.test_2340_auditeur_role`, `tests.test_2345_land_retry_bound`, `tests.test_2354_swap_sans_psi`, `tests.test_2377_revue_famille`, `tests.test_2400_train_migrations`, `tests.test_2426_packet_close`, `tests.test_2428_radar_clone_lock`, `tests.test_2432_land_head_clonebusy`, `tests.test_2442_verdict_carry`, `tests.test_2443_packet_members`, `tests.test_2446_train_initial`, `tests.test_2448_release_cutoff`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_2526_orphan_dependance`, `tests.test_2541_force_replay_mails`, `tests.test_2542_freeze_checks`, `tests.test_2580_partage_comptes`, `tests.test_2582_alerts_console_timeout`, `tests.test_2584_backlog_panier`, `tests.test_2601_packet_watch`, `tests.test_2623_orphan_levee_dependance`, `tests.test_2644_prevalidation_calls`, `tests.test_2681_review_reading`, `tests.test_2689_reveil_statut`, `tests.test_2739_review_correction`, `tests.test_2776_journal_designations`, `tests.test_2808_author_at_freeze`, `tests.test_2812_superseded_corrector`, `tests.test_2839_codex_account_gauges`, `tests.test_2851_alerts_court`, `tests.test_2877_stop_sweep_without_lock`, `tests.test_2888_reviewer_fix_independence`, `tests.test_2908_train_packet_vehicle`, `tests.test_2928_verdict_provenance`, `tests.test_2931_packet_target`, `tests.test_2934_budget_pause_notes`, `tests.test_2951_github_observation`, `tests.test_2954_incident_unique_ref`, `tests.test_2962_freeze_unresolved`, `tests.test_2971_freeze_note_marks`, `tests.test_3007_notify_fallback`, `tests.test_3036_alert_liveness`, `tests.test_3056_panier_3038`, `tests.test_3057_panier_vivacite`, `tests.test_3080_fire_queue_landing`, `tests.test_3085_nonconclusive_gates`, `tests.test_3104_verdict_receipt`, `tests.test_3111_legacy_git_domain`, `tests.test_3115_gate_hold_lease_ttl`, `tests.test_3123_volatile_hold`, `tests.test_313_comptes_deux_meshes`, `tests.test_3183_review_floor`, `tests.test_3185_relit_hors_equipe`, `tests.test_3186_unconcluded_aggregate`, `tests.test_3221_nonconclusive_key`, `tests.test_3236_brief_fenetre`, `tests.test_3244_release_optional_jobs`, `tests.test_3288_fixes_owed`, `tests.test_3291_staging_pending`, `tests.test_3307_basket_3300`, `tests.test_3331_canon_repo`, `tests.test_3333_blocked_freeze_holds`, `tests.test_3337_presence_hors_executeur`, `tests.test_3338_carried_corrector_trace`, `tests.test_3344_map_stale`, `tests.test_3364_packet_watch_doctor`, `tests.test_3366_train_canon_requalify`, `tests.test_3373_hold_lookalike`, `tests.test_339_orphan_settle`, `tests.test_3408_watch_foreign_pin`, `tests.test_3411_ephemeres_hors_designation`, `tests.test_3415_designation_dead_pause`, `tests.test_3416_review_correction_preview`, `tests.test_3447_prise_externe`, `tests.test_3480_ephemeres_hors_prise`, `tests.test_3495_dead_mail`, `tests.test_3504_mail_send_stdin`, `tests.test_3509_local_handoff`, `tests.test_3512_slo_merged`, `tests.test_3527_release_identity`, `tests.test_3530_ci_nonconclusive_dedup`, `tests.test_3532_fire_requeue_takeout`, `tests.test_3535_flux_tolerance`, `tests.test_3550_fire_queue_takeout_scope`, `tests.test_3556_auto_take_holds`, `tests.test_3557_review_floor_designation`, `tests.test_3572_canon_lowering_panier`, `tests.test_3578_verdict_family_receipt`, `tests.test_3589_stacked_freeze`, `tests.test_3592_floor_unreadable`, `tests.test_3632_merge_observed_vehicle`, `tests.test_3752_gabarit`, `tests.test_3770_handoff_child_stdin`, `tests.test_3845_paquet_horaire`, `tests.test_4037_reviewed_requalification`, `tests.test_4046_requalify_verdict_lookup`, `tests.test_4071_prevalidation_guest`, `tests.test_501_unleased_registry`, `tests.test_508_runner_liveness`, `tests.test_743_exec_only_serve`, `tests.test_789_slo_alerts`, `tests.test_802_canon_sync_concurrency`, `tests.test_803_classes_famille`, `tests.test_810_multi_project_consumers`, `tests.test_bin_launchers`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l119_backlog`, `tests.test_l120_harness_versions`, `tests.test_l130_demandes_sans_lot`, `tests.test_l132_alert_briefs`, `tests.test_l142_env_isole`, `tests.test_l148_stage1a1_alias`, `tests.test_l148_stage1a2_alias`, `tests.test_l159d_host_measure`, `tests.test_l160b_radar`, `tests.test_l160e2_land`, `tests.test_l160f_versions`, `tests.test_l160g_nexlink`, `tests.test_l161f_plan`, `tests.test_l161g_ci_gel`, `tests.test_l165_ep3_election_standby`, `tests.test_l165_ep4_pilot_gesture`, `tests.test_l165_prise_lots`, `tests.test_l165b_agents_libres`, `tests.test_l225_prix_claude`, `tests.test_l232b_release_authority`, `tests.test_l232e_drain_handoffs`, `tests.test_l233b_budget_handoff`, `tests.test_l234_auditor`, `tests.test_l234a_auditor_canon_guard`, `tests.test_l242b2_budget`, `tests.test_l253_disk_volumes`, `tests.test_l317_project_modules`, `tests.test_l317c_deep_analysis`, `tests.test_l31_orphans`, `tests.test_l31_resources`, `tests.test_l368_planned_waits`, `tests.test_l480_chat_veille`, `tests.test_l50_group_orphan_work_items`, `tests.test_l513_deepseek_offpeak`, `tests.test_l52_persona_sessions`, `tests.test_l539_review_lot`, `tests.test_l59_activity`, `tests.test_l64_unmeasured`, `tests.test_l72_panne_de_base`, `tests.test_l780_outdated_runner`, `tests.test_l795_preemption_evenement`, `tests.test_l79_chat_terminal`, `tests.test_l84_serve`, `tests.test_l85_events`, `tests.test_l95_budget_juste`, `tests.test_migrations`, `tests.test_models_entrypoint`, `tests.test_path_b_assembly`.

Modules voisins joués (support importé, carte d'impact) : carte d'impact `6d67072c3787` (lue, antérieure à la base) ; budget de carte 180 s par unité, 897 s pris, 924 module(s) au-delà du budget non joué(s).

| unité | module | raison | fichiers de l'unité |
|---|---|---|---|
| unité 3981 | `tests.test_3085_nonconclusive_gates` | support | `tests/test_2448_release_cutoff.py` |
| unité 3981 | `tests.test_3186_unconcluded_aggregate` | support | `tests/test_2448_release_cutoff.py` |
| unité 3981 | `tests.test_3244_release_optional_jobs` | support | `tests/test_2448_release_cutoff.py` |
| unité 3981 | `tests.test_3527_release_identity` | support | `tests/test_2448_release_cutoff.py` |
| unité 3981 | `tests.test_3530_ci_nonconclusive_dedup` | support | `tests/test_2448_release_cutoff.py` |
| unité 3981 | `tests.test_3535_flux_tolerance` | support | `tests/test_2448_release_cutoff.py` |
| unité 3981 | `tests.test_1002_relance_perimee` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_313_comptes_deux_meshes` | carte | `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_1075_pace_multiplier` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_l148_stage1a1_alias` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_1127_config_explicite` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_3115_gate_hold_lease_ttl` | carte | `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_3236_brief_fenetre` | carte | `src/ameesh/events.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2951_github_observation` | carte | `src/ameesh/events.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_3123_volatile_hold` | carte | `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_508_runner_liveness` | carte | `src/ameesh/events.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2580_partage_comptes` | carte | `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_743_exec_only_serve` | carte | `src/ameesh/events.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_1022_creneau_landed` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_3057_panier_vivacite` | carte | `src/ameesh/events.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_3036_alert_liveness` | carte | `src/ameesh/events.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_1901_gate_unreadable` | carte | `src/ameesh/events.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2211_roadmap_followups` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_l142_env_isole` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_1227_suite_1176` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_3408_watch_foreign_pin` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2851_alerts_court` | carte | `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_1391_backlog_route` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2345_land_retry_bound` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2200_pause_visible` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_l159d_host_measure` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2839_codex_account_gauges` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_3007_notify_fallback` | carte | `src/ameesh/events.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_l31_orphans` | carte | `src/ameesh/events.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_l52_persona_sessions` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_l64_unmeasured` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2354_swap_sans_psi` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_3589_stacked_freeze` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_l95_budget_juste` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_3504_mail_send_stdin` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_l233b_budget_handoff` | carte | `src/ameesh/events.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_3447_prise_externe` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_bin_launchers` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_l165_ep4_pilot_gesture` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_l165_prise_lots` | carte | `src/ameesh/events.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_1580_widget_forfait` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_3337_presence_hors_executeur` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_l368_planned_waits` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_l59_activity` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/events.py` |
| unité 3981 | `tests.test_2601_packet_watch` | carte | `src/ameesh/release_cutoff.py` |
| unité 3981 | `tests.test_3221_nonconclusive_key` | carte | `src/ameesh/release_cutoff.py` |
| unité 4071 | `tests.test_1770_mechanics_prevalidation` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_1820_plan_df_conflict` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_1770_prevalidation` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_l148_stage1a2_alias` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_1770_prevalidation_store` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_2328_ci_target_base` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_1770_prevalidation_cli` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_2644_prevalidation_calls` | carte | `src/ameesh/prevalidation.py`, `src/ameesh/prevalidation_calls.py` |
| unité 4071 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_1391_backlog_route` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_2200_pause_visible` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_l159d_host_measure` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_l64_unmeasured` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_2354_swap_sans_psi` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_3589_stacked_freeze` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_l95_budget_juste` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_3447_prise_externe` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_3337_presence_hors_executeur` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_l368_planned_waits` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_2931_packet_target` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_l120_harness_versions` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_2962_freeze_unresolved` | carte | `src/ameesh/prevalidation.py`, `src/ameesh/prevalidation_calls.py` |
| unité 4071 | `tests.test_l160b_radar` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_1193_suite_dependency` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_3373_hold_lookalike` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_3288_fixes_owed` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_l31_resources` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_2426_packet_close` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_l253_disk_volumes` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_2442_verdict_carry` | carte | `src/ameesh/prevalidation.py`, `src/ameesh/prevalidation_calls.py` |
| unité 4071 | `tests.test_2102_detacher_branche` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_2022_fleet` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_1819_no_replace_objects` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_models_entrypoint` | carte | `src/ameesh/prevalidation.py` |
| unité 4071 | `tests.test_1400_declaration_fusion` | carte | `src/ameesh/prevalidation.py` |
| unité 4082 | `tests.test_3512_slo_merged` | support | `tests/test_789_slo_alerts.py` |
| unité 4082 | `tests.test_3632_merge_observed_vehicle` | support | `tests/test_789_slo_alerts.py` |
| unité 4082 | `tests.test_1647_liste_lots_journal` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_3333_blocked_freeze_holds` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_2951_github_observation` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_3344_map_stale` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_1901_gate_unreadable` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l160f_versions` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_2851_alerts_court` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l225_prix_claude` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_2584_backlog_panier` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_3007_notify_fallback` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l31_orphans` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_3480_ephemeres_hors_prise` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_2354_swap_sans_psi` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l95_budget_juste` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l233b_budget_handoff` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_3447_prise_externe` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l119_backlog` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l165_prise_lots` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l368_planned_waits` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_2623_orphan_levee_dependance` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_339_orphan_settle` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_1677_prise_empilee` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l513_deepseek_offpeak` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l165b_agents_libres` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_3288_fixes_owed` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_501_unleased_registry` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l242b2_budget` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_1466_pause_longue` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l165_ep3_election_standby` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_3495_dead_mail` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_1272_rejeu_seul_affiche` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l234_auditor` | carte | `src/ameesh/planned_waits.py` |
| unité 4082 | `tests.test_l234a_auditor_canon_guard` | carte | `src/ameesh/planned_waits.py` |
| unité 4084 | `tests.test_1467_train` | support | `tests/test_l160e2_land.py` |
| unité 4084 | `tests.test_1467_train_transports` | support | `tests/test_l160e2_land.py` |
| unité 4084 | `tests.test_1651_ci_parts` | support | `tests/test_l160e2_land.py` |
| unité 4084 | `tests.test_1656_auto_merge` | support | `tests/test_l160e2_land.py` |
| unité 4084 | `tests.test_1682_auto_merge_constat` | support | `tests/test_l160e2_land.py` |
| unité 4084 | `tests.test_1718_land_eject_unreadable` | support | `tests/test_l160e2_land.py` |
| unité 4084 | `tests.test_2286_queue_required_checks` | support | `tests/test_l160e2_land.py` |
| unité 4084 | `tests.test_2345_land_retry_bound` | support | `tests/test_l160e2_land.py` |
| unité 4084 | `tests.test_2428_radar_clone_lock` | support | `tests/test_l160e2_land.py` |
| unité 4084 | `tests.test_2432_land_head_clonebusy` | support | `tests/test_l160e2_land.py` |
| unité 4084 | `tests.test_3080_fire_queue_landing` | support | `tests/test_l160e2_land.py` |
| unité 4084 | `tests.test_l160g_nexlink` | support | `tests/test_l160e2_land.py` |
| unité 3519 | `tests.test_3770_handoff_child_stdin` | support | `tests/test_3509_local_handoff.py` |
| unité 4046 | `tests.test_1002_relance_perimee` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_path_b_assembly` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1075_pace_multiplier` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_l148_stage1a1_alias` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1467_train_storage` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2446_train_initial` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2681_review_reading` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2908_train_packet_vehicle` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1127_config_explicite` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2254_radar_passes` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2776_journal_designations` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2954_incident_unique_ref` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3185_relit_hors_equipe` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_803_classes_famille` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_l132_alert_briefs` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_l79_chat_terminal` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1680_packet_reservation` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3411_ephemeres_hors_designation` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1647_liste_lots_journal` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2377_revue_famille` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3333_blocked_freeze_holds` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3578_verdict_family_receipt` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2951_github_observation` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_508_runner_liveness` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2971_freeze_note_marks` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3550_fire_queue_takeout_scope` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2130_d_projection` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_l161g_ci_gel` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1467_train_fire` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2812_superseded_corrector` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_743_exec_only_serve` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1022_creneau_landed` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1467_train_cli` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2334_nexlink_paquet` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3057_panier_vivacite` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3111_legacy_git_domain` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3036_alert_liveness` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3344_map_stale` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2877_stop_sweep_without_lock` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_l780_outdated_runner` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1467_train_runtime` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3104_verdict_receipt` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3592_floor_unreadable` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1901_gate_unreadable` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2888_reviewer_fix_independence` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3338_carried_corrector_trace` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3415_designation_dead_pause` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1053_famille_au_gel` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2928_verdict_provenance` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3366_train_canon_requalify` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3056_panier_3038` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_l160f_versions` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1467_train_events` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2211_roadmap_followups` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_l795_preemption_evenement` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_802_canon_sync_concurrency` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_l480_chat_veille` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3183_review_floor` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3572_canon_lowering_panier` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3532_fire_requeue_takeout` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_l539_review_lot` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_l142_env_isole` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1227_suite_1176` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2808_author_at_freeze` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_l232e_drain_handoffs` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3331_canon_repo` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3408_watch_foreign_pin` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_3556_auto_take_holds` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2400_train_migrations` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2851_alerts_court` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2339_autotake_migration` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_l232b_release_authority` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_1391_backlog_route` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2345_land_retry_bound` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2541_force_replay_mails` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2739_review_correction` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4046 | `tests.test_2200_pause_visible` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4073 | `tests.test_3104_verdict_receipt` | carte | `src/ameesh/freeze_checks.py`, `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_1391_backlog_route` | carte | `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_3416_review_correction_preview` | carte | `src/ameesh/freeze_checks.py`, `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_3589_stacked_freeze` | carte | `src/ameesh/freeze_checks.py`, `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_3447_prise_externe` | carte | `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_l368_planned_waits` | carte | `src/ameesh/freeze_checks.py`, `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_2931_packet_target` | carte | `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_2962_freeze_unresolved` | carte | `src/ameesh/freeze_checks.py`, `src/ameesh/mesh_cli.py`, `src/ameesh/prevalidation_calls.py` |
| unité 4073 | `tests.test_1193_suite_dependency` | carte | `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_3373_hold_lookalike` | carte | `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_3288_fixes_owed` | carte | `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_2426_packet_close` | carte | `src/ameesh/freeze_checks.py`, `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_2442_verdict_carry` | carte | `src/ameesh/freeze_checks.py`, `src/ameesh/mesh_cli.py`, `src/ameesh/prevalidation_calls.py` |
| unité 4073 | `tests.test_2102_detacher_branche` | carte | `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_l130_demandes_sans_lot` | carte | `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_3557_review_floor_designation` | carte | `src/ameesh/freeze_checks.py`, `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_l317_project_modules` | carte | `src/ameesh/freeze_checks.py`, `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_l84_serve` | carte | `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_1280_work_close_done` | carte | `src/ameesh/freeze_checks.py`, `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_2443_packet_members` | carte | `src/ameesh/freeze_checks.py`, `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_l317c_deep_analysis` | carte | `src/ameesh/mesh_cli.py` |
| unité 4073 | `tests.test_3364_packet_watch_doctor` | carte | `src/ameesh/freeze_checks.py` |
| unité 4073 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/prevalidation_calls.py` |
| unité 4073 | `tests.test_3307_basket_3300` | carte | `src/ameesh/freeze_checks.py` |
| unité 4073 | `tests.test_2377_revue_famille` | carte | `src/ameesh/freeze_checks.py` |

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
