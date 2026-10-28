# Paquet integration/paquet-h-20261028-05 — composition (§12), sur main `6acdd9274563`

Base : `main` **`6acdd92745638b527ee608583f3be8d629d3a20a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261028-05`, cible `main`.
Porteur : **#4170**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #3526 | `7279d6959d38c4b5fa284fd7d2e7f68956adbcfa` |
| #4139 | `c533641fddc883b60b27f4ac56a7f3700324898a` |
| #4159 | `3b741d02fb8fa80c3400fc871a510416ea35d024` |
| #4161 | `d1edb660c6d7905087000b77fcc984024de08f95` |
| #4162 | `761cfc58d7e098015ae9e080c1b340f50e5c0bdc` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3526 | `7279d6959d38` | `37eb237abbc9` | composition |
| 2 | #4139 | `c533641fddc8` | `9e04e36dca8c` | composition |
| 3 | #4159 | `3b741d02fb8f` | `4fc4d49cbfdb` | composition |
| 4 | #4161 | `d1edb660c6d7` | `ed5cd946990b` | composition |
| 5 | #4162 | `761cfc58d7e0` | `51a0c1ebbf5f` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 5 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 18.4 s.
- Tests locaux : 6 séries, 171 modules, 2682 tests exécutés, 0 échec(s), 0 run(s) rouge(s) sans effet, 0 non concluant(s), 14 sauté(s) : `tests.test_1002_relance_perimee`, `tests.test_1021_tenue_attribution`, `tests.test_1022_creneau_landed`, `tests.test_1053_famille_au_gel`, `tests.test_1075_pace_multiplier`, `tests.test_1127_config_explicite`, `tests.test_1227_suite_1176`, `tests.test_1272_rejeu_seul_affiche`, `tests.test_1286_declared_merge_proof`, `tests.test_1391_backlog_route`, `tests.test_1400_declaration_fusion`, `tests.test_1425_stop_sweep_suites`, `tests.test_1467_train_events`, `tests.test_1467_train_incidents`, `tests.test_1467_train_policy`, `tests.test_1580_widget_forfait`, `tests.test_1647_liste_lots_journal`, `tests.test_1749_zombies_git`, `tests.test_1770_mechanics_prevalidation`, `tests.test_1901_gate_unreadable`, `tests.test_2104_radar_attribution`, `tests.test_2167_host_housekeeping`, `tests.test_2200_pause_visible`, `tests.test_2211_roadmap_followups`, `tests.test_2254_radar_passes`, `tests.test_2265_critical_policy_drift`, `tests.test_2296_admission_pression_memoire`, `tests.test_2334_nexlink_paquet`, `tests.test_2340_auditeur_role`, `tests.test_2345_land_retry_bound`, `tests.test_2354_swap_sans_psi`, `tests.test_2377_revue_famille`, `tests.test_2388_radar_stacked_migration`, `tests.test_2428_radar_clone_lock`, `tests.test_2432_land_head_clonebusy`, `tests.test_2446_train_assembly`, `tests.test_2446_train_contract`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_2464_prets_cible_release`, `tests.test_246_btrfs_guard`, `tests.test_2473_fire_readiness_repo`, `tests.test_2505_fire_classe_base`, `tests.test_2526_orphan_dependance`, `tests.test_2535_fire_repo_explicite`, `tests.test_2582_alerts_console_timeout`, `tests.test_2644_prevalidation_calls`, `tests.test_2673_radar_clone_target`, `tests.test_2680_declare_target`, `tests.test_2689_reveil_statut`, `tests.test_2776_journal_designations`, `tests.test_2792_ci_plan_review`, `tests.test_2808_author_at_freeze`, `tests.test_2812_superseded_corrector`, `tests.test_2839_codex_account_gauges`, `tests.test_2851_alerts_court`, `tests.test_2877_stop_sweep_without_lock`, `tests.test_2888_reviewer_fix_independence`, `tests.test_2908_train_packet_vehicle`, `tests.test_2914_train_canon_drift`, `tests.test_2928_verdict_provenance`, `tests.test_2934_budget_pause_notes`, `tests.test_2951_github_observation`, `tests.test_2954_incident_unique_ref`, `tests.test_2968_review_after_merge_repo`, `tests.test_2971_freeze_note_marks`, `tests.test_2973_process_scope`, `tests.test_3003_reorder_relative_order`, `tests.test_3007_notify_fallback`, `tests.test_3035_host_reconcile`, `tests.test_3056_panier_3038`, `tests.test_3068_classe_cible_distante`, `tests.test_3085_nonconclusive_gates`, `tests.test_3098_train_contract_pins`, `tests.test_3104_verdict_receipt`, `tests.test_3108_train_contract_jobs`, `tests.test_3111_legacy_git_domain`, `tests.test_3115_gate_hold_lease_ttl`, `tests.test_3180_host_cli_actor`, `tests.test_3183_review_floor`, `tests.test_3185_relit_hors_equipe`, `tests.test_3275_review_after_merge_fetch`, `tests.test_3292_debit`, `tests.test_3307_basket_3300`, `tests.test_3318_provenance_remote`, `tests.test_3323_not_in_target_canonique`, `tests.test_3331_canon_repo`, `tests.test_3333_blocked_freeze_holds`, `tests.test_3337_presence_hors_executeur`, `tests.test_3338_carried_corrector_trace`, `tests.test_3344_map_stale`, `tests.test_3345_profils_runner`, `tests.test_3350_canon_repository`, `tests.test_3364_packet_watch_doctor`, `tests.test_3366_train_canon_requalify`, `tests.test_339_orphan_settle`, `tests.test_3408_watch_foreign_pin`, `tests.test_3411_ephemeres_hors_designation`, `tests.test_3415_designation_dead_pause`, `tests.test_3416_vehicle_head_fetch`, `tests.test_3437_debit_dependance`, `tests.test_3447_prise_externe`, `tests.test_3483_train_canon_auto`, `tests.test_3503_jit`, `tests.test_3504_mail_send_stdin`, `tests.test_3509_local_handoff`, `tests.test_3512_slo_merged`, `tests.test_3526_runtime_seal`, `tests.test_3527_release_identity`, `tests.test_3529_nexlink_release`, `tests.test_3532_fire_requeue_takeout`, `tests.test_3540_git_agents`, `tests.test_3550_fire_queue_takeout_scope`, `tests.test_3572_canon_lowering_panier`, `tests.test_3581_ci_node`, `tests.test_3589_stacked_freeze`, `tests.test_3592_floor_unreadable`, `tests.test_3606_train_pins`, `tests.test_3619_host_crypto`, `tests.test_3632_merge_observed_vehicle`, `tests.test_3752_gabarit`, `tests.test_3770_handoff_child_stdin`, `tests.test_4134_process_admission`, `tests.test_508_runner_liveness`, `tests.test_743_exec_only_serve`, `tests.test_802_canon_sync_concurrency`, `tests.test_803_classes_famille`, `tests.test_810_multi_project_consumers`, `tests.test_811_lot_workdirs`, `tests.test_bin_launchers`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l119_backlog`, `tests.test_l1372_projection_brief`, `tests.test_l142_env_isole`, `tests.test_l148_stage1a1_alias`, `tests.test_l159c_plafonds_memoire`, `tests.test_l159d_host_measure`, `tests.test_l160f_versions`, `tests.test_l1616_mechanics`, `tests.test_l161f_plan`, `tests.test_l165_ep4_pilot_gesture`, `tests.test_l165_prise_lots`, `tests.test_l165c_sessions_par_lot`, `tests.test_l1757_paquets_horaires`, `tests.test_l225_prix_claude`, `tests.test_l232b_bootstrap`, `tests.test_l232b_bootstrap_files`, `tests.test_l232b_entree_operateur`, `tests.test_l233b_budget_handoff`, `tests.test_l234_auditor`, `tests.test_l234a_auditor_canon_guard`, `tests.test_l242c_critical_roles`, `tests.test_l31_orphans`, `tests.test_l50_group_orphan_work_items`, `tests.test_l52_persona_sessions`, `tests.test_l539_review_lot`, `tests.test_l64_unmeasured`, `tests.test_l66_desktop`, `tests.test_l68_macos_ci`, `tests.test_l72_panne_de_base`, `tests.test_l750_release`, `tests.test_l756_full_suite`, `tests.test_l77_resident_queue`, `tests.test_l780_outdated_runner`, `tests.test_l95_budget_juste`, `tests.test_migrations`, `tests.test_oidc`, `tests.test_plan_git`, `tests.test_process_card`, `tests.test_version`.

Modules voisins joués (support importé, carte d'impact) : carte d'impact `6d67072c3787` (lue, antérieure à la base) ; budget de carte 180 s par unité, 358 s pris, 487 module(s) au-delà du budget non joué(s).

| unité | module | raison | fichiers de l'unité |
|---|---|---|---|
| unité 3526 | `tests.test_3770_handoff_child_stdin` | support | `tests/test_3509_local_handoff.py` |
| unité 3526 | `tests.test_l232b_bootstrap_files` | support | `tests/test_l232b_bootstrap.py` |
| unité 4159 | `tests.test_2446_train_contract` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2914_train_canon_drift` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l756_full_suite` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1770_mechanics_prevalidation` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2265_critical_policy_drift` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3606_train_pins` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1272_rejeu_seul_affiche` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2505_fire_classe_base` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3540_git_agents` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1749_zombies_git` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3619_host_crypto` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l159c_plafonds_memoire` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l234_auditor` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3318_provenance_remote` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3323_not_in_target_canonique` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3350_canon_repository` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3364_packet_watch_doctor` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_process_card` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l234a_auditor_canon_guard` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3437_debit_dependance` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l68_macos_ci` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3307_basket_3300` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3529_nexlink_release` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_version` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2464_prets_cible_release` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2908_train_packet_vehicle` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3345_profils_runner` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l1757_paquets_horaires` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2254_radar_passes` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2776_journal_designations` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2954_incident_unique_ref` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3185_relit_hors_equipe` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3581_ci_node` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_803_classes_famille` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l77_resident_queue` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3115_gate_hold_lease_ttl` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3411_ephemeres_hors_designation` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1400_declaration_fusion` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1647_liste_lots_journal` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2377_revue_famille` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2792_ci_plan_review` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3003_reorder_relative_order` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3292_debit` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3333_blocked_freeze_holds` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2951_github_observation` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_508_runner_liveness` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2971_freeze_note_marks` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3416_vehicle_head_fetch` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3550_fire_queue_takeout_scope` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l1616_mechanics` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2473_fire_readiness_repo` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2812_superseded_corrector` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3098_train_contract_pins` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3275_review_after_merge_fetch` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2334_nexlink_paquet` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2432_land_head_clonebusy` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2446_train_assembly` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3111_legacy_git_domain` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1286_declared_merge_proof` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2535_fire_repo_explicite` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3344_map_stale` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l242c_critical_roles` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_plan_git` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2877_stop_sweep_without_lock` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l780_outdated_runner` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3104_verdict_receipt` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3592_floor_unreadable` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l66_desktop` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_oidc` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1467_train_incidents` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1901_gate_unreadable` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2888_reviewer_fix_independence` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3503_jit` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2428_radar_clone_lock` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_246_btrfs_guard` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3338_carried_corrector_trace` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3415_designation_dead_pause` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3483_train_canon_auto` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3527_release_identity` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1053_famille_au_gel` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2680_declare_target` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2928_verdict_provenance` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3366_train_canon_requalify` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3056_panier_3038` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l160f_versions` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1467_train_events` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2973_process_scope` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3108_train_contract_jobs` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2104_radar_attribution` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3035_host_reconcile` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1467_train_policy` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2388_radar_stacked_migration` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2644_prevalidation_calls` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3180_host_cli_actor` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_802_canon_sync_concurrency` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l750_release` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3183_review_floor` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3572_canon_lowering_panier` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3068_classe_cible_distante` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3532_fire_requeue_takeout` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_l539_review_lot` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2673_radar_clone_target` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2968_review_after_merge_repo` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1227_suite_1176` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_2808_author_at_freeze` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/canon.py` |
| unité 4159 | `tests.test_3331_canon_repo` | carte | `src/ameesh/canon.py` |
| unité 4162 | `tests.test_811_lot_workdirs` | support | `tests/test_l165c_sessions_par_lot.py` |
| unité 4162 | `tests.test_1002_relance_perimee` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_1075_pace_multiplier` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_l148_stage1a1_alias` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_1127_config_explicite` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_2951_github_observation` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_743_exec_only_serve` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_1022_creneau_landed` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_3344_map_stale` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_1901_gate_unreadable` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_2211_roadmap_followups` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_l142_env_isole` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_1227_suite_1176` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_3408_watch_foreign_pin` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_2851_alerts_court` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_1391_backlog_route` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_2345_land_retry_bound` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_2200_pause_visible` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_l225_prix_claude` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_l159d_host_measure` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_2839_codex_account_gauges` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_3007_notify_fallback` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_3512_slo_merged` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_810_multi_project_consumers` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_l31_orphans` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_l52_persona_sessions` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_l64_unmeasured` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_2354_swap_sans_psi` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_3589_stacked_freeze` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_l95_budget_juste` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_3504_mail_send_stdin` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_2689_reveil_statut` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_l233b_budget_handoff` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_3447_prise_externe` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_bin_launchers` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_l119_backlog` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_3632_merge_observed_vehicle` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_l165_ep4_pilot_gesture` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_l165_prise_lots` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_1580_widget_forfait` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |
| unité 4162 | `tests.test_3337_presence_hors_executeur` | carte | `src/ameesh/storage/postgres/lot_sessions.py` |

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
