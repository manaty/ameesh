-- SPDX-License-Identifier: AGPL-3.0-only
-- ameesh: non additive
-- #7652: manual per-database owner approval required; no business rows removed.
DO $p3$
DECLARE
    core text := current_schema();
    mod text;
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
    original_path text;
    old_text text;
    new_text text;
    pair text[];
    acl record;
    later boolean;
BEGIN
    EXECUTE format('SELECT schema_name FROM %I.module_schemas WHERE module = ''dev''', core)
        INTO mod;
    IF mod IS NULL OR core = mod OR NOT EXISTS (
        SELECT FROM pg_namespace WHERE nspname = mod) THEN
        RAISE EXCEPTION 'dev/0002 rollback needs an applied P2 registry';
    END IF;
    EXECUTE format('SELECT EXISTS (SELECT FROM %I.module_schema_migrations '
                   'WHERE module = ''dev'' AND version > 2)', core) INTO later;
    IF later THEN
        RAISE EXCEPTION 'dev/0002 rollback requires rollback of later migrations first';
    END IF;
    FOREACH t IN ARRAY names LOOP
        IF to_regclass(format('%I.%I', core, t)) IS NOT NULL THEN
            RAISE EXCEPTION 'dev/0002 rollback destination exists: %.%', core, t;
        END IF;
        SELECT c.oid, c.relowner INTO rel FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
         WHERE n.nspname=mod AND c.relname=t AND c.relkind='r';
        IF rel.oid IS NULL THEN
            RAISE EXCEPTION 'dev/0002 rollback missing table %.%', mod, t;
        END IF;
        EXECUTE format('CREATE VIEW %I.%I AS SELECT * FROM %I.%I',
                       core, t, mod, t);
        -- A new view inherits default ACLs: clear them before copying exactly
        -- the DML grants (including column grants and WITH GRANT OPTION).
        FOR acl IN SELECT a.grantee FROM pg_class c,
                LATERAL aclexplode(c.relacl) a
            WHERE c.oid = to_regclass(format('%I.%I', core, t))
        LOOP
            target := CASE WHEN acl.grantee = 0 THEN 'PUBLIC'
                           ELSE quote_ident(pg_get_userbyid(acl.grantee)) END;
            EXECUTE format('REVOKE ALL ON %I.%I FROM %s', core, t, target);
        END LOOP;
        EXECUTE format('ALTER VIEW %I.%I OWNER TO %I', core, t, pg_get_userbyid(rel.relowner));
        FOR acl IN
            SELECT a.grantee, a.privilege_type, a.is_grantable, NULL::text AS col
              FROM pg_class c,
                   LATERAL aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a
             WHERE c.oid = rel.oid
            UNION ALL
            SELECT a.grantee, a.privilege_type, a.is_grantable, att.attname AS col
              FROM pg_attribute att, LATERAL aclexplode(att.attacl) a
             WHERE att.attrelid = rel.oid AND att.attnum > 0 AND NOT att.attisdropped
        LOOP
            IF acl.privilege_type NOT IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE') THEN
                CONTINUE;
            END IF;
            target := CASE WHEN acl.grantee = 0 THEN 'PUBLIC'
                           ELSE quote_ident(pg_get_userbyid(acl.grantee)) END;
            EXECUTE format('GRANT %s%s ON %I.%I TO %s%s',
                           acl.privilege_type,
                           CASE WHEN acl.col IS NULL THEN '' ELSE format(' (%I)', acl.col) END,
                           core, t, target,
                           CASE WHEN acl.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);
        END LOOP;
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
        target := old_text;
        old_text := new_text;
        new_text := target;
        SELECT pg_get_functiondef(p.oid),
               (SELECT substring(setting FROM 13) FROM unnest(p.proconfig) setting
                 WHERE setting LIKE 'search_path=%')
          INTO definition, original_path FROM pg_proc p
         WHERE p.oid=to_regprocedure(format('%I.%I()', core, fn));
        IF definition IS NULL OR strpos(definition, old_text) = 0 THEN
            RAISE EXCEPTION 'dev/0002 unrecognised function %: missing %', fn, old_text;
        END IF;
        EXECUTE replace(definition, old_text, new_text);
        IF original_path IS NOT NULL THEN
            EXECUTE format('ALTER FUNCTION %I.%I() SET search_path TO %L', core, fn, original_path);
        END IF;
    END LOOP;
    EXECUTE format('DELETE FROM %I.module_schema_migrations WHERE module = ''dev'' AND version = 2', core);
END
$p3$;
