-- SPDX-License-Identifier: AGPL-3.0-only
-- ameesh: non additive
-- #7651 / #7616 §5.3 : exception de transition déclarée, objets du noyau
-- déplacés vers dev ; aucune ligne métier copiée, modifiée ou supprimée.
-- Go owner PAR BASE, écrivains arrêtés, sauvegarde restaurée et rollback
-- éprouvé avant ce geste. Le runner tient transaction, lock_timeout et bail.
DO $cutover$
DECLARE
    mod text := current_schema();
    core text;
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
    rel record;
    acl record;
    target text;
    definition text;
    original text := 's       text := tg_table_schema;';
    patched text := 's       text := coalesce(tg_argv[0], tg_table_schema);';
BEGIN
    core := (current_schemas(false))[2];
    IF core IS NULL OR core = mod THEN
        RAISE EXCEPTION 'dev/0001 needs module, core search_path';
    END IF;
    -- Validate the runner's registry rather than deriving/truncating a name.
    EXECUTE format('SELECT schema_name FROM %I.module_schemas WHERE module = ''dev''', core)
        INTO target;
    IF target IS DISTINCT FROM mod THEN
        RAISE EXCEPTION 'dev/0001 registry and search_path disagree';
    END IF;

    -- Preflight the ENTIRE inventory before altering the first relation.
    FOREACH t IN ARRAY names LOOP
        SELECT c.oid, c.relkind, c.relrowsecurity, c.relforcerowsecurity
          INTO rel FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = core AND c.relname = t;
        IF rel.oid IS NULL OR rel.relkind <> 'r' THEN
            RAISE EXCEPTION 'dev/0001 needs ordinary table %.%', core, t;
        END IF;
        -- An unexpected policy needs its own review; never silently bypass it.
        IF rel.relrowsecurity OR rel.relforcerowsecurity OR EXISTS (
                SELECT FROM pg_policy WHERE polrelid = rel.oid) THEN
            RAISE EXCEPTION 'dev/0001 unexpected RLS on %.%', core, t;
        END IF;
        IF to_regclass(format('%I.%I', mod, t)) IS NOT NULL THEN
            RAISE EXCEPTION 'dev/0001 destination already exists: %.%', mod, t;
        END IF;
    END LOOP;

    -- Preserve schema access for roles that already use the core, including
    -- sequence-only users. USAGE grants no relation privilege of its own.
    FOR acl IN SELECT a.* FROM pg_namespace n,
            LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
        WHERE n.nspname = core AND a.privilege_type = 'USAGE'
    LOOP
        target := CASE WHEN acl.grantee = 0 THEN 'PUBLIC'
                       ELSE quote_ident(pg_get_userbyid(acl.grantee)) END;
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO %s', mod, target);
    END LOOP;

    -- Keep the original function, OID, owner, ACL and fixed search_path.
    -- Only its choice of registry schema changes; all existing guards remain.
    SELECT pg_get_functiondef(to_regprocedure(format('%I.ameesh_limited_login_guard()', core)))
        INTO definition;
    IF definition IS NULL OR strpos(definition, original) = 0 THEN
        RAISE EXCEPTION 'dev/0001 unrecognised limited-login guard; review required';
    END IF;
    EXECUTE replace(definition, original, patched);

    FOREACH t IN ARRAY names LOOP
        SELECT c.oid, c.relowner INTO rel
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = core AND c.relname = t;
        EXECUTE format('ALTER TABLE %I.%I SET SCHEMA %I', core, t, mod);
        -- RLS was rejected above. The view keeps the table owner and exact
        -- column ACLs: security_invoker would require SELECT on every column
        -- of SELECT *, breaking column-limited readers even for one column.
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
    -- Triggers follow table/function OIDs; only this one needs a core argument.
    EXECUTE format('DROP TRIGGER zz_ameesh_limited_login ON %I.provenance_receipts', mod);
    EXECUTE format('CREATE TRIGGER zz_ameesh_limited_login BEFORE INSERT OR UPDATE OR DELETE'
                   ' ON %I.provenance_receipts FOR EACH ROW'
                   ' EXECUTE FUNCTION %I.ameesh_limited_login_guard(%L)', mod, core, core);
    -- merge_queue_generation_seq is intentionally NOT moved (owner Q7).
END
$cutover$;
