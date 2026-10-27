# Paquet integration/paquet-h-20261027-45 — composition (§12), sur main `d3e86101ec81`

Base : `main` **`d3e86101ec81c40c3eb0f2e729b29768e3d5ea2f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261027-45`, cible `main`.
Porteur : **#4076**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (8 unités, 8 lots)

| lot | source composée |
|---|---|
| #3986 | `15f3d8ee26724196fdac6669fd95e6e94e656011` |
| #4050 | `c72d1d940c643ba93af89febc6364cfcd34490b8` |
| #4053 | `27804a8bc102c03314b67b5661d5906068a18520` |
| #4056 | `736f613cc33e52f72881085f955551529a30f383` |
| #4063 | `0a9e5d57d508facdf4a99a9376fcb978903a802f` |
| #4066 | `e3e298165bbb8b83ace5b788e51416a8f1d3d178` |
| #4067 | `df1324799b67e4bbedc72fbd3f89339d56377f33` |
| #4048 | `489251affe082c5841e247a6cf2a9d7bc057342a` |

## Composition (8 unités, 8 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3986 | `15f3d8ee2672` | `75d111e3843f` | composition |
| 2 | #4050 | `c72d1d940c64` | `18049a21848c` | composition |
| 3 | #4053 | `27804a8bc102` | `f58001a41485` | composition |
| 4 | #4056 | `736f613cc33e` | `e5f36bafcefb` | composition |
| 5 | #4063 | `0a9e5d57d508` | `6a93751de730` | composition |
| 6 | #4066 | `e3e298165bbb` | `33024ee5a143` | composition |
| 7 | #4067 | `df1324799b67` | `ade37e9002a2` | composition |
| 8 | #4048 | `489251affe08` | `db30cbeb1238` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 8 lot(s) prêt(s), 6 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 17.1 s.
- Tests locaux : 9 séries, 198 modules, 5079 tests exécutés, 0 échec(s), 0 non concluant(s), 10 sauté(s) : `tests.test_1002_relance_perimee`, `tests.test_1021_tenue_attribution`, `tests.test_1022_creneau_landed`, `tests.test_1053_famille_au_gel`, `tests.test_1075_pace_multiplier`, `tests.test_1127_config_explicite`, `tests.test_1155_staging`, `tests.test_1193_suite_dependency`, `tests.test_1227_suite_1176`, `tests.test_1272_rejeu_seul_affiche`, `tests.test_1318_verdict_perime`, `tests.test_1323_actif`, `tests.test_1391_backlog_route`, `tests.test_1400_declaration_fusion`, `tests.test_1425_stop_sweep_suites`, `tests.test_1467_train_candidates`, `tests.test_1467_train_cli`, `tests.test_1467_train_events`, `tests.test_1467_train_fire`, `tests.test_1467_train_runtime`, `tests.test_1467_train_storage`, `tests.test_1647_liste_lots_journal`, `tests.test_1677_prise_empilee`, `tests.test_1680_packet_reservation`, `tests.test_1681_incident`, `tests.test_1737_wake_deadline`, `tests.test_1770_prevalidation_cli`, `tests.test_1822_classe_legere`, `tests.test_1901_gate_unreadable`, `tests.test_2003_reap_volumes`, `tests.test_2022_fleet`, `tests.test_2076_multiple_reviewers`, `tests.test_2130_d_projection`, `tests.test_2167_host_housekeeping`, `tests.test_2200_pause_visible`, `tests.test_2211_roadmap_followups`, `tests.test_2233_underuse_limits`, `tests.test_2254_radar_passes`, `tests.test_2256_fire_landing_path`, `tests.test_2296_admission_pression_memoire`, `tests.test_2328_ci_target_base`, `tests.test_2334_nexlink_paquet`, `tests.test_2339_autotake_migration`, `tests.test_2340_auditeur_role`, `tests.test_2345_land_retry_bound`, `tests.test_2354_swap_sans_psi`, `tests.test_2377_revue_famille`, `tests.test_2400_train_migrations`, `tests.test_2426_packet_close`, `tests.test_2442_verdict_carry`, `tests.test_2446_train_initial`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_2487_designation_carry`, `tests.test_2526_orphan_dependance`, `tests.test_2541_force_replay_mails`, `tests.test_2552_revue_source`, `tests.test_2556_tmp_copies_once`, `tests.test_2582_alerts_console_timeout`, `tests.test_2584_backlog_panier`, `tests.test_2585_review_already_done`, `tests.test_2623_orphan_levee_dependance`, `tests.test_2644_prevalidation_calls`, `tests.test_2648_next_choice_underuse`, `tests.test_2681_review_reading`, `tests.test_2689_reveil_statut`, `tests.test_2702_class_at_merge_paquet`, `tests.test_2739_review_correction`, `tests.test_2743_housekeeping_clones`, `tests.test_2776_journal_designations`, `tests.test_2803_packet_authors`, `tests.test_2808_author_at_freeze`, `tests.test_2812_superseded_corrector`, `tests.test_2813_packet_auto_merge`, `tests.test_2851_alerts_court`, `tests.test_2864_proposition_familles`, `tests.test_2877_stop_sweep_without_lock`, `tests.test_2888_reviewer_fix_independence`, `tests.test_2908_train_packet_vehicle`, `tests.test_2928_verdict_provenance`, `tests.test_2931_packet_target`, `tests.test_2934_budget_pause_notes`, `tests.test_2951_github_observation`, `tests.test_2954_incident_unique_ref`, `tests.test_2962_freeze_unresolved`, `tests.test_2971_freeze_note_marks`, `tests.test_2994_reorder_and_prefix_survival`, `tests.test_3007_notify_fallback`, `tests.test_3022_prod_gate`, `tests.test_3036_alert_liveness`, `tests.test_3056_panier_3038`, `tests.test_3057_panier_vivacite`, `tests.test_3066_packet_required_checks`, `tests.test_3085_nonconclusive_gates`, `tests.test_3104_verdict_receipt`, `tests.test_3111_legacy_git_domain`, `tests.test_313_comptes_deux_meshes`, `tests.test_3162_slo_start_facts`, `tests.test_3183_review_floor`, `tests.test_3185_relit_hors_equipe`, `tests.test_3209_skipped_by_construction`, `tests.test_3221_nonconclusive_key`, `tests.test_3236_brief_fenetre`, `tests.test_3288_fixes_owed`, `tests.test_3293_cache_pressure`, `tests.test_3301_prior_freeze_unknown`, `tests.test_3308_gh_base_ref`, `tests.test_3331_canon_repo`, `tests.test_3333_blocked_freeze_holds`, `tests.test_3335_corrector_survival`, `tests.test_3337_presence_hors_executeur`, `tests.test_3338_carried_corrector_trace`, `tests.test_3344_map_stale`, `tests.test_3366_train_canon_requalify`, `tests.test_3373_hold_lookalike`, `tests.test_3396_housekeeping_unreadable`, `tests.test_339_orphan_settle`, `tests.test_3408_watch_foreign_pin`, `tests.test_3411_ephemeres_hors_designation`, `tests.test_3415_designation_dead_pause`, `tests.test_3416_review_correction_preview`, `tests.test_3447_prise_externe`, `tests.test_3480_ephemeres_hors_prise`, `tests.test_3512_slo_merged`, `tests.test_3518_test_only_corrector`, `tests.test_3530_ci_nonconclusive_dedup`, `tests.test_3532_fire_requeue_takeout`, `tests.test_3550_fire_queue_takeout_scope`, `tests.test_3554_canon_lowering`, `tests.test_3555_slo_hold_after_go`, `tests.test_3556_auto_take_holds`, `tests.test_3558_inherited_red`, `tests.test_3565_hold_race`, `tests.test_3572_canon_lowering_panier`, `tests.test_3578_verdict_family_receipt`, `tests.test_3589_stacked_freeze`, `tests.test_3592_floor_unreadable`, `tests.test_3632_merge_observed_vehicle`, `tests.test_3752_gabarit`, `tests.test_3986_qa_freeze`, `tests.test_4025_paquet_voisins`, `tests.test_508_runner_liveness`, `tests.test_705_reviewer_designated`, `tests.test_743_exec_only_serve`, `tests.test_753_stop_returns_lots`, `tests.test_789_slo`, `tests.test_789_slo_alerts`, `tests.test_802_canon_sync_concurrency`, `tests.test_803_classes_famille`, `tests.test_810_multi_project_consumers`, `tests.test_811_lot_workdirs`, `tests.test_bout_en_bout`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l119_backlog`, `tests.test_l120_harness_versions`, `tests.test_l132_alert_briefs`, `tests.test_l142_env_isole`, `tests.test_l148_stage1a1_alias`, `tests.test_l148_stage1a2_alias`, `tests.test_l148_stage3b_workstream_readers`, `tests.test_l159d_host_measure`, `tests.test_l160b_radar`, `tests.test_l160f_versions`, `tests.test_l161f_plan`, `tests.test_l161g_ci_gel`, `tests.test_l165_prise_lots`, `tests.test_l165b_agents_libres`, `tests.test_l165c_sessions_par_lot`, `tests.test_l225_prix_claude`, `tests.test_l232b_release_authority`, `tests.test_l232e_drain_handoffs`, `tests.test_l233b_budget_handoff`, `tests.test_l253_disk_volumes`, `tests.test_l26_exploitation`, `tests.test_l317b_module_audit`, `tests.test_l31_orphans`, `tests.test_l31_resources`, `tests.test_l368_planned_waits`, `tests.test_l480_chat_veille`, `tests.test_l50_group_orphan_work_items`, `tests.test_l539_review_lot`, `tests.test_l64_unmeasured`, `tests.test_l68_macos_ci`, `tests.test_l71_comptabilite`, `tests.test_l72_panne_de_base`, `tests.test_l744_ci_entrypoint`, `tests.test_l755_workflow_timeouts`, `tests.test_l780_outdated_runner`, `tests.test_l795_preemption_evenement`, `tests.test_l79_chat_terminal`, `tests.test_l804_prise_executeur`, `tests.test_l83_chat_api`, `tests.test_l95_budget_juste`, `tests.test_migrations`, `tests.test_models_entrypoint`, `tests.test_path_b_assembly`, `tests.test_plan`.

Modules voisins joués (support importé, carte d'impact) : carte d'impact `6d67072c3787` (lue, antérieure à la base) ; budget de carte 180 s par unité, 892 s pris, 1066 module(s) au-delà du budget non joué(s).

| unité | module | raison | fichiers de l'unité |
|---|---|---|---|
| unité 3986 | `tests.test_2426_packet_close` | support | `tests/test_plan.py` |
| unité 3986 | `tests.test_3022_prod_gate` | support | `tests/test_plan.py` |
| unité 3986 | `tests.test_3565_hold_race` | support | `tests/test_1677_prise_empilee.py` |
| unité 3986 | `tests.test_3632_merge_observed_vehicle` | support | `tests/test_789_slo_alerts.py` |
| unité 3986 | `tests.test_811_lot_workdirs` | support | `tests/test_l165c_sessions_par_lot.py` |
| unité 3986 | `tests.test_l71_comptabilite` | support | `tests/test_l26_exploitation.py` |
| unité 3986 | `tests.test_3162_slo_start_facts` | carte | `src/ameesh/progress.py` |
| unité 3986 | `tests.test_789_slo` | carte | `src/ameesh/progress.py` |
| unité 3986 | `tests.test_803_classes_famille` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_l132_alert_briefs` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_2003_reap_volumes` | carte | `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_3236_brief_fenetre` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_3293_cache_pressure` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_3411_ephemeres_hors_designation` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_1647_liste_lots_journal` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_2377_revue_famille` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_2556_tmp_copies_once` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_3333_blocked_freeze_holds` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_2951_github_observation` | carte | `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_3057_panier_vivacite` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_3036_alert_liveness` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_3344_map_stale` | carte | `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_1901_gate_unreadable` | carte | `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py` … |
| unité 3986 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py` … |
| unité 3986 | `tests.test_l160f_versions` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py` … |
| unité 3986 | `tests.test_3556_auto_take_holds` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_2851_alerts_court` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py` … |
| unité 3986 | `tests.test_2339_autotake_migration` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_2200_pause_visible` | carte | `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_2864_proposition_familles` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_l225_prix_claude` | carte | `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_2584_backlog_panier` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_3007_notify_fallback` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py` … |
| unité 3986 | `tests.test_3396_housekeeping_unreadable` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_l31_orphans` | carte | `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_3480_ephemeres_hors_prise` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_2354_swap_sans_psi` | carte | `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_l95_budget_juste` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py` … |
| unité 3986 | `tests.test_2552_revue_source` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_l233b_budget_handoff` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py` … |
| unité 3986 | `tests.test_3447_prise_externe` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py` … |
| unité 3986 | `tests.test_l119_backlog` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py` … |
| unité 3986 | `tests.test_l165_prise_lots` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py` … |
| unité 3986 | `tests.test_2743_housekeeping_clones` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_3337_presence_hors_executeur` | carte | `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_l368_planned_waits` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py` … |
| unité 3986 | `tests.test_2623_orphan_levee_dependance` | carte | `src/ameesh/progress.py`, `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py` … |
| unité 3986 | `tests.test_339_orphan_settle` | carte | `src/ameesh/stagnation.py`, `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py` … |
| unité 3986 | `tests.test_l317b_module_audit` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_l804_prise_executeur` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_1002_relance_perimee` | carte | `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_2648_next_choice_underuse` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_3066_packet_required_checks` | carte | `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_2233_underuse_limits` | carte | `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_3308_gh_base_ref` | carte | `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_1737_wake_deadline` | carte | `src/ameesh/stagnation.py` |
| unité 3986 | `tests.test_313_comptes_deux_meshes` | carte | `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_1075_pace_multiplier` | carte | `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 3986 | `tests.test_1272_rejeu_seul_affiche` | carte | `src/ameesh/stagnation.py` |
| unité 3986 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/storage/postgres/lot_sessions.py`, `src/ameesh/storage/postgres/operations.py`, `src/ameesh/storage/postgres/work_dependencies.py` |
| unité 4053 | `tests.test_3331_canon_repo` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2541_force_replay_mails` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2487_designation_carry` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2776_journal_designations` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2377_revue_famille` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2812_superseded_corrector` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_3111_legacy_git_domain` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_3592_floor_unreadable` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2888_reviewer_fix_independence` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_3338_carried_corrector_trace` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_3415_designation_dead_pause` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2808_author_at_freeze` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2739_review_correction` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2702_class_at_merge_paquet` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2864_proposition_familles` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_3416_review_correction_preview` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_3554_canon_lowering` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2552_revue_source` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_3335_corrector_survival` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_3301_prior_freeze_unknown` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_1467_train_candidates` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_3565_hold_race` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_3518_test_only_corrector` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2931_packet_target` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2994_reorder_and_prefix_survival` | carte | `src/ameesh/verdict_carry.py` |
| unité 4053 | `tests.test_2803_packet_authors` | carte | `src/ameesh/verdict_carry.py` |
| unité 4056 | `tests.test_2908_train_packet_vehicle` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2776_journal_designations` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_803_classes_famille` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3411_ephemeres_hors_designation` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2377_revue_famille` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3333_blocked_freeze_holds` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3578_verdict_family_receipt` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2812_superseded_corrector` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3111_legacy_git_domain` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2877_stop_sweep_without_lock` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3592_floor_unreadable` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2888_reviewer_fix_independence` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3338_carried_corrector_trace` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3415_designation_dead_pause` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_1053_famille_au_gel` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2928_verdict_provenance` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_l795_preemption_evenement` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_l480_chat_veille` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3183_review_floor` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3572_canon_lowering_panier` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_l539_review_lot` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2808_author_at_freeze` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_l232e_drain_handoffs` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3331_canon_repo` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3556_auto_take_holds` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2851_alerts_court` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_l232b_release_authority` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_1391_backlog_route` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2541_force_replay_mails` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2739_review_correction` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2200_pause_visible` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2702_class_at_merge_paquet` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2864_proposition_familles` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_l83_chat_api` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3416_review_correction_preview` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2584_backlog_panier` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3007_notify_fallback` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3396_housekeeping_unreadable` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3512_slo_merged` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3554_canon_lowering` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_753_stop_returns_lots` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3480_ephemeres_hors_prise` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2585_review_already_done` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2552_revue_source` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3335_corrector_survival` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3301_prior_freeze_unknown` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_l233b_budget_handoff` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3447_prise_externe` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_l119_backlog` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_1002_relance_perimee` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_2256_fire_landing_path` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3221_nonconclusive_key` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3530_ci_nonconclusive_dedup` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3558_inherited_red` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_1681_incident` | carte | `src/ameesh/work.py` |
| unité 4056 | `tests.test_3209_skipped_by_construction` | carte | `src/ameesh/work.py` |
| unité 4066 | `tests.test_1002_relance_perimee` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_path_b_assembly` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1075_pace_multiplier` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_l148_stage1a1_alias` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1467_train_storage` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2446_train_initial` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2681_review_reading` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2908_train_packet_vehicle` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1127_config_explicite` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2254_radar_passes` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2776_journal_designations` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2954_incident_unique_ref` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3185_relit_hors_equipe` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_803_classes_famille` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_l132_alert_briefs` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_l79_chat_terminal` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1680_packet_reservation` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3411_ephemeres_hors_designation` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1647_liste_lots_journal` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2377_revue_famille` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3333_blocked_freeze_holds` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3578_verdict_family_receipt` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2951_github_observation` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_508_runner_liveness` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2971_freeze_note_marks` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3550_fire_queue_takeout_scope` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2130_d_projection` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_l161g_ci_gel` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1467_train_fire` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2812_superseded_corrector` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_743_exec_only_serve` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1022_creneau_landed` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1467_train_cli` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2334_nexlink_paquet` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3057_panier_vivacite` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3111_legacy_git_domain` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3036_alert_liveness` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3344_map_stale` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2877_stop_sweep_without_lock` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_l780_outdated_runner` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1467_train_runtime` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3104_verdict_receipt` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3592_floor_unreadable` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1901_gate_unreadable` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2888_reviewer_fix_independence` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3338_carried_corrector_trace` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3415_designation_dead_pause` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1053_famille_au_gel` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2928_verdict_provenance` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3366_train_canon_requalify` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3056_panier_3038` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_l160f_versions` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1467_train_events` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2211_roadmap_followups` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_l795_preemption_evenement` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_802_canon_sync_concurrency` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_l480_chat_veille` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3183_review_floor` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3572_canon_lowering_panier` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3532_fire_requeue_takeout` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_l539_review_lot` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_l142_env_isole` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1227_suite_1176` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2808_author_at_freeze` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_l232e_drain_handoffs` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3331_canon_repo` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3408_watch_foreign_pin` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3556_auto_take_holds` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2400_train_migrations` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2851_alerts_court` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2339_autotake_migration` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_l232b_release_authority` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_1391_backlog_route` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2345_land_retry_bound` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2541_force_replay_mails` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2739_review_correction` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_2200_pause_visible` | carte | `src/ameesh/handoff.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4066 | `tests.test_3162_slo_start_facts` | carte | `src/ameesh/handoff.py` |
| unité 4066 | `tests.test_3555_slo_hold_after_go` | carte | `src/ameesh/handoff.py` |
| unité 4066 | `tests.test_789_slo` | carte | `src/ameesh/handoff.py` |
| unité 4066 | `tests.test_l148_stage3b_workstream_readers` | carte | `src/ameesh/handoff.py` |
| unité 4067 | `tests.test_2328_ci_target_base` | support | `tests/test_l161g_ci_gel.py` |
| unité 4067 | `tests.test_2022_fleet` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_l148_stage1a2_alias` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_models_entrypoint` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_1400_declaration_fusion` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_1770_prevalidation_cli` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_3104_verdict_receipt` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_l744_ci_entrypoint` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_1391_backlog_route` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_2200_pause_visible` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_3416_review_correction_preview` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_l159d_host_measure` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_l64_unmeasured` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_2354_swap_sans_psi` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_3589_stacked_freeze` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_l95_budget_juste` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_2813_packet_auto_merge` | carte | `src/ameesh/ci.py`, `src/ameesh/prevalidation_calls.py` |
| unité 4067 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_3447_prise_externe` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_3337_presence_hors_executeur` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_l368_planned_waits` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_2931_packet_target` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_l120_harness_versions` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_2962_freeze_unresolved` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/prevalidation_calls.py` |
| unité 4067 | `tests.test_l160b_radar` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_1193_suite_dependency` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_3373_hold_lookalike` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_3288_fixes_owed` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_l31_resources` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_2426_packet_close` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_l253_disk_volumes` | carte | `src/ameesh/mesh_cli.py` |
| unité 4067 | `tests.test_2442_verdict_carry` | carte | `src/ameesh/mesh_cli.py`, `src/ameesh/prevalidation_calls.py` |
| unité 4067 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/prevalidation_calls.py` |

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
