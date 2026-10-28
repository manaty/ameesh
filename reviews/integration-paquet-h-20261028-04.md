# Paquet integration/paquet-h-20261028-04 — composition (§12), sur main `4c58991629e4`

Base : `main` **`4c58991629e435675cf47987b34db8f788eae134`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261028-04`, cible `main`.
Porteur : **#4153**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #4144 | `9a033fb6aaa6e6018e3ce5fd0d5646569fffbef1` |
| #4100 | `ccd79789957d661e9eb45e160a41872b4f704aaa` |
| #4113 | `c0358d7a21a5a214e3e8d79a55033a5042d478d9` |
| #4134 | `d595485ee979fb2855a66860f1e75879b9fb19e9` |
| #4147 | `2f7718d98bd6f3f15287de14a0c15c0814c52b34` |
| #4152 | `ad081b01dfc8d068aa0c1fccb693d7fbe7f96cc2` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4144 | `9a033fb6aaa6` | `f25640da6194` | composition |
| 2 | #4100 | `ccd79789957d` | `114b6ffee7d4` | composition |
| 3 | #4113 | `c0358d7a21a5` | `803402e5a7ab` | composition |
| 4 | #4134 | `d595485ee979` | `3fbc5ff931b7` | composition |
| 5 | #4147 | `2f7718d98bd6` | `6d93a5edb6c2` | composition |
| 6 | #4152 | `ad081b01dfc8` | `6f80dc9c61a7` | composition |

## Écartés

| lot | motif |
|---|---|
| #3526 | retiré : ci-full rouge attribuée à son unité (#3801) — --exclude, recomposition sans lui |

Liste calculée : 6 lot(s) prêt(s), 3 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 30.8 s.
- Tests locaux : 7 séries, 152 modules, 2689 tests exécutés, 0 échec(s), 0 run(s) rouge(s) sans effet, 0 non concluant(s), 18 sauté(s) : `tests.test_1002_relance_perimee`, `tests.test_1021_tenue_attribution`, `tests.test_1022_creneau_landed`, `tests.test_1053_famille_au_gel`, `tests.test_1075_pace_multiplier`, `tests.test_1127_config_explicite`, `tests.test_1190_dryrun_cross_cache_inode`, `tests.test_1227_suite_1176`, `tests.test_1266_unreadable_turn_window`, `tests.test_1286_declared_merge_proof`, `tests.test_1391_backlog_route`, `tests.test_1400_declaration_fusion`, `tests.test_1425_stop_sweep_suites`, `tests.test_1466_relecture_config`, `tests.test_1467_train_cli`, `tests.test_1467_train_events`, `tests.test_1467_train_fire`, `tests.test_1467_train_runtime`, `tests.test_1467_train_storage`, `tests.test_1647_liste_lots_journal`, `tests.test_1680_packet_reservation`, `tests.test_1703_writer_identity`, `tests.test_1901_gate_unreadable`, `tests.test_2003_reap_volumes`, `tests.test_2022_disk_early_warning`, `tests.test_2022_fleet`, `tests.test_2022_host_zombies`, `tests.test_2130_d_projection`, `tests.test_2167_host_housekeeping`, `tests.test_2200_pause_visible`, `tests.test_2211_roadmap_followups`, `tests.test_2212_fire_fresh_radar`, `tests.test_2254_radar_passes`, `tests.test_2273_merge_pr_required`, `tests.test_2296_admission_pression_memoire`, `tests.test_2334_nexlink_paquet`, `tests.test_2339_autotake_migration`, `tests.test_2340_auditeur_role`, `tests.test_2341_host_unsampled`, `tests.test_2345_land_retry_bound`, `tests.test_2377_revue_famille`, `tests.test_2400_train_migrations`, `tests.test_2404_paquet_candidats`, `tests.test_2446_train_initial`, `tests.test_2449_garde_mecanique`, `tests.test_2457_garde_non_python`, `tests.test_2464_prets_cible_release`, `tests.test_246_btrfs_guard`, `tests.test_2481_multi_migration_reservations`, `tests.test_2526_orphan_dependance`, `tests.test_2541_force_replay_mails`, `tests.test_2556_tmp_copies_once`, `tests.test_2582_alerts_console_timeout`, `tests.test_2601_packet_watch`, `tests.test_2681_review_reading`, `tests.test_2702_class_at_merge_paquet`, `tests.test_2738_clone_objects`, `tests.test_2739_review_correction`, `tests.test_2766_vehicle_reviews`, `tests.test_2776_journal_designations`, `tests.test_2792_ci_plan_review`, `tests.test_2808_author_at_freeze`, `tests.test_2812_superseded_corrector`, `tests.test_2851_alerts_court`, `tests.test_2877_stop_sweep_without_lock`, `tests.test_2879_alerts_disk_trend`, `tests.test_2888_reviewer_fix_independence`, `tests.test_2908_train_packet_vehicle`, `tests.test_2928_verdict_provenance`, `tests.test_2931_packet_target`, `tests.test_2933_housekeeping_alert_identity`, `tests.test_2934_budget_pause_notes`, `tests.test_2951_github_observation`, `tests.test_2954_incident_unique_ref`, `tests.test_2971_freeze_note_marks`, `tests.test_3036_alert_liveness`, `tests.test_3041_origin_relative`, `tests.test_3056_panier_3038`, `tests.test_3057_panier_vivacite`, `tests.test_3066_packet_required_checks`, `tests.test_3085_nonconclusive_gates`, `tests.test_3091_packet_member_base`, `tests.test_3104_verdict_receipt`, `tests.test_3111_legacy_git_domain`, `tests.test_3183_review_floor`, `tests.test_3185_relit_hors_equipe`, `tests.test_3293_cache_pressure`, `tests.test_3331_canon_repo`, `tests.test_3333_blocked_freeze_holds`, `tests.test_3338_carried_corrector_trace`, `tests.test_3344_map_stale`, `tests.test_3366_train_canon_requalify`, `tests.test_339_orphan_settle`, `tests.test_3408_watch_foreign_pin`, `tests.test_3411_ephemeres_hors_designation`, `tests.test_3415_designation_dead_pause`, `tests.test_3503_jit`, `tests.test_3532_fire_requeue_takeout`, `tests.test_3533_production_provenance`, `tests.test_3550_fire_queue_takeout_scope`, `tests.test_3556_auto_take_holds`, `tests.test_3572_canon_lowering_panier`, `tests.test_3578_verdict_family_receipt`, `tests.test_3592_floor_unreadable`, `tests.test_3643_host_critical_names`, `tests.test_3752_gabarit`, `tests.test_3845_paquet_horaire`, `tests.test_3885_fingerprints`, `tests.test_4113_packet_epoch`, `tests.test_4134_process_admission`, `tests.test_4147_host_footprint`, `tests.test_4152_paquet_test_logs`, `tests.test_508_runner_liveness`, `tests.test_653_db_connections`, `tests.test_711_migration_reservations`, `tests.test_743_exec_only_serve`, `tests.test_802_canon_sync_concurrency`, `tests.test_803_classes_famille`, `tests.test_cluster`, `tests.test_events_attach`, `tests.test_l122_numeros_de_migration`, `tests.test_l132_alert_briefs`, `tests.test_l142_env_isole`, `tests.test_l148_stage1a1_alias`, `tests.test_l159a_capacity_guard`, `tests.test_l159c_plafonds_memoire`, `tests.test_l159d_host_measure`, `tests.test_l159e6_guard_restart_alert`, `tests.test_l159e_guard`, `tests.test_l159i_vm_supervisor`, `tests.test_l160f_versions`, `tests.test_l161f_plan`, `tests.test_l161g_ci_gel`, `tests.test_l232b_release_authority`, `tests.test_l232e_drain_handoffs`, `tests.test_l253_disk_volumes`, `tests.test_l31_orphans`, `tests.test_l31_resources`, `tests.test_l411_docker_storage`, `tests.test_l480_chat_veille`, `tests.test_l50_group_orphan_work_items`, `tests.test_l539_review_lot`, `tests.test_l64_unmeasured`, `tests.test_l66_desktop`, `tests.test_l72_panne_de_base`, `tests.test_l73_housekeeping`, `tests.test_l780_outdated_runner`, `tests.test_l795_preemption_evenement`, `tests.test_l79_chat_terminal`, `tests.test_l84_serve`, `tests.test_migrations`, `tests.test_path_b_assembly`.

Modules voisins joués (support importé, carte d'impact) : carte d'impact `6d67072c3787` (lue, antérieure à la base) ; budget de carte 180 s par unité, 419 s pris, 503 module(s) au-delà du budget non joué(s).

| unité | module | raison | fichiers de l'unité |
|---|---|---|---|
| unité 4144 | `tests.test_2464_prets_cible_release` | carte | `src/ameesh/packet_candidates.py` |
| unité 4144 | `tests.test_2601_packet_watch` | carte | `src/ameesh/packet_candidates.py` |
| unité 4144 | `tests.test_2792_ci_plan_review` | carte | `src/ameesh/packet_candidates.py` |
| unité 4144 | `tests.test_3183_review_floor` | carte | `src/ameesh/packet_candidates.py` |
| unité 4144 | `tests.test_2766_vehicle_reviews` | carte | `src/ameesh/packet_candidates.py` |
| unité 4144 | `tests.test_3041_origin_relative` | carte | `src/ameesh/packet_candidates.py` |
| unité 4144 | `tests.test_2738_clone_objects` | carte | `src/ameesh/packet_candidates.py` |
| unité 4144 | `tests.test_2931_packet_target` | carte | `src/ameesh/packet_candidates.py` |
| unité 4144 | `tests.test_3091_packet_member_base` | carte | `src/ameesh/packet_candidates.py` |
| unité 4144 | `tests.test_3533_production_provenance` | carte | `src/ameesh/packet_candidates.py` |
| unité 4100 | `tests.test_1022_creneau_landed` | support | `tests/test_l122_numeros_de_migration.py` |
| unité 4100 | `tests.test_2400_train_migrations` | support | `tests/test_l122_numeros_de_migration.py` |
| unité 4100 | `tests.test_2481_multi_migration_reservations` | support | `tests/test_l122_numeros_de_migration.py` |
| unité 4100 | `tests.test_3885_fingerprints` | support | `tests/test_l122_numeros_de_migration.py` |
| unité 4100 | `tests.test_711_migration_reservations` | support | `tests/test_l122_numeros_de_migration.py` |
| unité 4100 | `tests.test_1002_relance_perimee` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_path_b_assembly` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_1075_pace_multiplier` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_l148_stage1a1_alias` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_1467_train_storage` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_3085_nonconclusive_gates` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2446_train_initial` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2681_review_reading` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2908_train_packet_vehicle` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_1127_config_explicite` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_2254_radar_passes` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2776_journal_designations` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2954_incident_unique_ref` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3185_relit_hors_equipe` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_803_classes_famille` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_l132_alert_briefs` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_l79_chat_terminal` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_1680_packet_reservation` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_3411_ephemeres_hors_designation` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_1400_declaration_fusion` | carte | `src/ameesh/migrations.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_1647_liste_lots_journal` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2377_revue_famille` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3333_blocked_freeze_holds` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3578_verdict_family_receipt` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2951_github_observation` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_508_runner_liveness` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2971_freeze_note_marks` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3550_fire_queue_takeout_scope` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2130_d_projection` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_l161g_ci_gel` | carte | `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_1467_train_fire` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_2812_superseded_corrector` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_743_exec_only_serve` | carte | `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_1467_train_cli` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_2334_nexlink_paquet` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3057_panier_vivacite` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_3111_legacy_git_domain` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_1286_declared_merge_proof` | carte | `src/ameesh/migrations.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3036_alert_liveness` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_3344_map_stale` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2877_stop_sweep_without_lock` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_l780_outdated_runner` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_1467_train_runtime` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_3104_verdict_receipt` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3592_floor_unreadable` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_1901_gate_unreadable` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2888_reviewer_fix_independence` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3338_carried_corrector_trace` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3415_designation_dead_pause` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_1053_famille_au_gel` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2928_verdict_provenance` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3366_train_canon_requalify` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_3056_panier_3038` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_l160f_versions` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_1467_train_events` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2211_roadmap_followups` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_l795_preemption_evenement` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_802_canon_sync_concurrency` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_l480_chat_veille` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3183_review_floor` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3572_canon_lowering_panier` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3532_fire_requeue_takeout` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_l539_review_lot` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_l142_env_isole` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_1227_suite_1176` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_2808_author_at_freeze` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_l232e_drain_handoffs` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_1425_stop_sweep_suites` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_3331_canon_repo` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_3408_watch_foreign_pin` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_3556_auto_take_holds` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_2851_alerts_court` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2339_autotake_migration` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py` |
| unité 4100 | `tests.test_l232b_release_authority` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_1391_backlog_route` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_2345_land_retry_bound` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_2541_force_replay_mails` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2739_review_correction` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2200_pause_visible` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/interface.py`, `src/ameesh/storage/postgres/work.py` … |
| unité 4100 | `tests.test_2702_class_at_merge_paquet` | carte | `src/ameesh/migrations.py`, `src/ameesh/storage/postgres/work.py`, `src/ameesh/work.py` |
| unité 4100 | `tests.test_2273_merge_pr_required` | carte | `src/ameesh/migrations.py` |
| unité 4100 | `tests.test_3066_packet_required_checks` | carte | `src/ameesh/migrations.py` |
| unité 4100 | `tests.test_1703_writer_identity` | carte | `src/ameesh/migrations.py` |
| unité 4100 | `tests.test_2212_fire_fresh_radar` | carte | `src/ameesh/migrations.py` |
| unité 4147 | `tests.test_l159e_guard` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_l159d_host_measure` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_l64_unmeasured` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_l31_resources` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_l253_disk_volumes` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_l159a_capacity_guard` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_l84_serve` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_l73_housekeeping` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_l411_docker_storage` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_1190_dryrun_cross_cache_inode` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2341_host_unsampled` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_1266_unreadable_turn_window` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2022_disk_early_warning` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2022_host_zombies` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_l159i_vm_supervisor` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_1466_relecture_config` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2022_fleet` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_l159c_plafonds_memoire` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_l159e6_guard_restart_alert` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2167_host_housekeeping` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_3643_host_critical_names` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2003_reap_volumes` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_3293_cache_pressure` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2556_tmp_copies_once` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2951_github_observation` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_653_db_connections` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2879_alerts_disk_trend` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_3344_map_stale` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_l66_desktop` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_1901_gate_unreadable` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_3503_jit` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_246_btrfs_guard` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_1021_tenue_attribution` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2582_alerts_console_timeout` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_l232e_drain_handoffs` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2526_orphan_dependance` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2934_budget_pause_notes` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2296_admission_pression_memoire` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2933_housekeeping_alert_identity` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2851_alerts_court` | carte | `src/ameesh/resources.py` |
| unité 4147 | `tests.test_2339_autotake_migration` | carte | `src/ameesh/resources.py` |

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
