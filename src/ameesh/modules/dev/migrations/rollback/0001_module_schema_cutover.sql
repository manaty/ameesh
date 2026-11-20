-- SPDX-License-Identifier: AGPL-3.0-only
-- #7651 : retour P2 -> P1, JAMAIS découvert/appliqué automatiquement.
-- Go owner, écrivains arrêtés ; search_path = noyau ; transaction unique.
BEGIN;
SET LOCAL lock_timeout = '5s';
SELECT pg_advisory_xact_lock(hashtext('ameesh_migrations'));
DO $rollback$
DECLARE
    core text := current_schema();
    mod text;
    names text[] := ARRAY[
        'ci_runs',
        'ci_test_failures',
        'ci_runner_state',
        'ci_queue_state',
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
    kind "char";
    later boolean;
    definition text;
    original text := 's       text := tg_table_schema;';
    patched text := 's       text := coalesce(tg_argv[0], tg_table_schema);';
BEGIN
    EXECUTE format('SELECT schema_name FROM %I.module_schemas WHERE module = ''dev''', core)
        INTO mod;
    IF mod IS NULL THEN
        RAISE EXCEPTION 'dev rollback needs an applied P2 registry';
    END IF;
    EXECUTE format('SELECT EXISTS (SELECT FROM %I.module_schema_migrations'
                   ' WHERE module = ''dev'' AND version > 1)', core) INTO later;
    IF later THEN
        RAISE EXCEPTION 'dev rollback P2 requires rollback of later migrations first';
    END IF;
    FOREACH t IN ARRAY names LOOP
        SELECT relkind INTO kind FROM pg_class
         WHERE oid = to_regclass(format('%I.%I', mod, t));
        IF kind IS DISTINCT FROM 'r' THEN
            RAISE EXCEPTION 'dev rollback needs table %.%', mod, t;
        END IF;
        SELECT relkind INTO kind FROM pg_class
         WHERE oid = to_regclass(format('%I.%I', core, t));
        IF kind IS NOT NULL AND kind <> 'v' THEN
            RAISE EXCEPTION 'dev rollback refuses non-view %.%', core, t;
        END IF;
    END LOOP;
    SELECT pg_get_functiondef(to_regprocedure(format('%I.ameesh_limited_login_guard()', core)))
        INTO definition;
    IF definition IS NULL OR strpos(definition, patched) = 0 THEN
        RAISE EXCEPTION 'dev rollback unrecognised limited-login guard';
    END IF;
    EXECUTE format('DROP TRIGGER zz_ameesh_limited_login ON %I.provenance_receipts', mod);
    EXECUTE format('CREATE TRIGGER zz_ameesh_limited_login BEFORE INSERT OR UPDATE OR DELETE'
                   ' ON %I.provenance_receipts FOR EACH ROW'
                   ' EXECUTE FUNCTION %I.ameesh_limited_login_guard()', mod, core);
    EXECUTE replace(definition, patched, original);
    FOREACH t IN ARRAY names LOOP
        EXECUTE format('DROP VIEW IF EXISTS %I.%I', core, t);
        EXECUTE format('ALTER TABLE %I.%I SET SCHEMA %I', mod, t, core);
    END LOOP;
    -- RESTRICT (default), never CASCADE: an unexpected remaining object
    -- aborts the WHOLE rollback, leaving P2 and its registry intact.
    EXECUTE format('DROP SCHEMA %I', mod);
    EXECUTE format('DELETE FROM %I.module_schema_non_additive_migrations'
                   ' WHERE module = ''dev'' AND version = 1', core);
    EXECUTE format('DELETE FROM %I.module_schema_migrations'
                   ' WHERE module = ''dev'' AND version = 1', core);
    EXECUTE format('DELETE FROM %I.module_schemas WHERE module = ''dev''', core);
END
$rollback$;
COMMIT;
