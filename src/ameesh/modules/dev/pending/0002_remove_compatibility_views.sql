-- SPDX-License-Identifier: AGPL-3.0-only
-- ameesh: non additive
-- #7652: manual per-database owner approval required; no business rows removed.
DO $p3$
DECLARE
    mod text := current_schema();
    core text := (current_schemas(false))[2];
    names text[] := ARRAY[
        'ci_runs',
        'ci_test_failures',
        'ci_runner_state',
        'ci_queue_state',
        'ci_queue_wait',
        'ci_queue_wait_scan',
        'ci_guard_scan',
        'ci_guard_run',
        'ci_map_state',
        'ci_disk_state',
        'ci_disk_refusals',
        'ci_disk_dispatch',
        'merge_queue',
        'merge_locks',
        'integration_trains',
        'integration_train_fires',
        'integration_train_queue_receipts',
        'integration_train_requalifications',
        'work_packet_members',
        'work_packet_snapshots',
        'work_landings',
        'work_resolutions',
        'work_migration_reservations',
        'managed_worktrees',
        'work_item_branch_heads',
        'prevalidation_reports',
        'github_watch_cursors',
        'github_watch_events',
        'github_watch_scans',
        'github_watch_pending',
        'review_catch_up',
        'review_finding_resolutions',
        'work_review_withdrawals',
        'provenance_receipts',
        'provenance_revocations',
        'work_item_footprints',
        'work_item_impact_analyses',
        'repository_footprint_policies',
        'work_item_measures'
    ];
    t text;
    target text;
    rel record;
    fn text;
    definition text;
    old_text text;
    new_text text;
    pair text[];
BEGIN
    IF core IS NULL OR core = mod OR NOT EXISTS (
        SELECT FROM pg_namespace WHERE nspname = mod) THEN
        RAISE EXCEPTION 'dev/0002 needs module, core search_path';
    END IF;
    EXECUTE format('SELECT schema_name FROM %I.module_schemas WHERE module = ''dev''', core)
        INTO target;
    IF target IS DISTINCT FROM mod THEN
        RAISE EXCEPTION 'dev/0002 registry and search_path disagree';
    END IF;
    FOREACH t IN ARRAY names LOOP
        SELECT c.relkind INTO rel FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
         WHERE n.nspname=core AND c.relname=t;
        IF rel.relkind IS DISTINCT FROM 'v' OR NOT EXISTS (
            SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname=mod AND c.relname=t AND c.relkind='r') THEN
            RAISE EXCEPTION 'dev/0002 expected view and table for %', t;
        END IF;
    END LOOP;
    -- Rewrite only relation positions; retain each function's existing settings,
    -- owner, ACL, OID and search_path. Refuse unknown definitions.
    FOREACH pair SLICE 1 IN ARRAY ARRAY[
        ARRAY['work_items_migration_slot_sync', 'update work_migration_reservations', 'work_migration_reservations'],
        ARRAY['work_items_migration_slot_sync', 'insert into work_migration_reservations', 'work_migration_reservations'],
        ARRAY['work_migration_reservation_guard', 'from work_migration_reservations', 'work_migration_reservations'],
        ARRAY['ameesh_review_seats_guard', 'update work_review_withdrawals', 'work_review_withdrawals'],
        ARRAY['ameesh_review_seats_guard', 'insert into work_review_withdrawals', 'work_review_withdrawals'],
        ARRAY['ameesh_review_verdict_guard', 'from work_review_withdrawals', 'work_review_withdrawals']
    ] LOOP
        fn := pair[1];
        old_text := pair[2];
        new_text := replace(old_text, pair[3], format('%I.%I', mod, pair[3]));
        SELECT pg_get_functiondef(p.oid) INTO definition FROM pg_proc p
         WHERE p.oid=to_regprocedure(format('%I.%I()', core, fn));
        IF definition IS NULL OR strpos(definition, old_text) = 0 THEN
            RAISE EXCEPTION 'dev/0002 unrecognised function %: missing %', fn, old_text;
        END IF;
        -- pg_get_functiondef carries SET search_path with each element quoted;
        -- re-applying the proconfig text as one literal would make it one schema.
        EXECUTE replace(definition, old_text, new_text);
    END LOOP;
    FOREACH t IN ARRAY names LOOP
        EXECUTE format('DROP VIEW %I.%I', core, t);
    END LOOP;
END
$p3$;
