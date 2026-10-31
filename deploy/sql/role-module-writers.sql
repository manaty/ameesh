-- SPDX-License-Identifier: AGPL-3.0-only
-- Apply after migration 0363, as the schema/migration owner.
-- These NOLOGIN groups belong to separate protected service logins, NEVER
-- to executor logins. Credentials and canon/code are isolated at the OS level.
-- Revoke PUBLIC TEMP on the database separately before applying this script.
-- The audit rejects TEMP on the groups and their transitive members, including
-- PUBLIC grants and other parent-role grants (also reachable by SET ROLE).
\set ON_ERROR_STOP on
BEGIN;
SELECT current_schema() AS schema \gset
SELECT 'CREATE ROLE ameesh_canon_sync NOLOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ameesh_canon_sync') \gexec
SELECT 'CREATE ROLE ameesh_it_proof NOLOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ameesh_it_proof') \gexec
-- L244 (#420): the QA verdict has its own isolated writer, distinct from the
-- execution proof writer.
SELECT 'CREATE ROLE ameesh_it_qa NOLOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ameesh_it_qa') \gexec
SELECT format('GRANT USAGE ON SCHEMA %I TO ameesh_canon_sync, ameesh_it_proof, ameesh_it_qa', :'schema') \gexec
-- Canon sync executes the existing registry/authenticator/package transaction.
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO ameesh_canon_sync', :'schema') \gexec
-- #4975: deleting the parent inventory would cascade into the marker.
SELECT format('REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON %I.schema_migrations FROM ameesh_canon_sync', :'schema')
 WHERE to_regclass(format('%I.schema_migrations', :'schema')) IS NOT NULL \gexec
-- #2274: the review catch-up activation bound is the owner's alone
-- (migration 0438); canon sync must not move it.
SELECT format('REVOKE INSERT, UPDATE, DELETE ON %I.%I FROM ameesh_canon_sync', :'schema', name)
  FROM unnest(ARRAY['schema_non_additive_migrations','review_catch_up','host_release_operations','host_release_fences','host_release_fence_receipts']) name
 WHERE to_regclass(format('%I.%I', :'schema', name)) IS NOT NULL \gexec
SELECT format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO ameesh_canon_sync', :'schema') \gexec
SELECT format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO ameesh_canon_sync, ameesh_it_proof, ameesh_it_qa', :'schema') \gexec
SELECT format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM ameesh_it_proof, ameesh_it_qa', :'schema') \gexec
SELECT format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM ameesh_it_proof, ameesh_it_qa', :'schema') \gexec
SELECT format('GRANT SELECT ON %I.work_items, %I.project_modules, %I.work_module_milestones TO ameesh_it_proof, ameesh_it_qa',
              :'schema', :'schema', :'schema') \gexec
-- #2481: a full work row projects its reserved migration slots
-- (`work.get`); the isolated writers read the lot through that projection.
SELECT format('GRANT SELECT ON %I.work_migration_reservations TO ameesh_it_proof, ameesh_it_qa', :'schema')
 WHERE to_regclass(format('%I.work_migration_reservations', :'schema')) IS NOT NULL \gexec
-- #4975: all isolated writers read the compatibility marker; none writes it.
SELECT format('GRANT SELECT ON %I.schema_non_additive_migrations TO ameesh_canon_sync, ameesh_it_proof, ameesh_it_qa', :'schema')
 WHERE to_regclass(format('%I.schema_non_additive_migrations', :'schema')) IS NOT NULL \gexec
-- SELECT FOR NO KEY UPDATE needs UPDATE on at least one column.
SELECT format('GRANT UPDATE (id) ON %I.work_items TO ameesh_it_proof, ameesh_it_qa', :'schema') \gexec
SELECT format('GRANT INSERT ON %I.work_module_milestones TO ameesh_it_proof, ameesh_it_qa', :'schema') \gexec
SELECT format('GRANT USAGE ON SEQUENCE %I.work_module_milestones_id_seq TO ameesh_it_proof, ameesh_it_qa', :'schema') \gexec
-- L244 (#420): the QA guard reads the actor's declared roles and projects
-- under a share lock through ameesh_module_agent (SECURITY DEFINER, fixed
-- search_path, migration 0427). The isolated role needs EXECUTE, not SELECT:
-- FOR SHARE on agent_registry requires UPDATE, which it must not have.
SELECT format('GRANT EXECUTE ON FUNCTION %I.ameesh_module_agent(text) TO ameesh_it_qa', :'schema')
 WHERE EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE n.nspname = :'schema' AND p.proname = 'ameesh_module_agent') \gexec
DO $audit$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM pg_roles WHERE rolname IN ('ameesh_it_proof', 'ameesh_it_qa', 'ameesh_canon_sync') LOOP
        IF r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolbypassrls OR r.rolreplication OR r.rolcanlogin THEN
            RAISE EXCEPTION 'module writer groups must be nonadministrative NOLOGIN roles';
        END IF;
        IF has_schema_privilege(r.oid, current_schema(), 'CREATE') THEN
            RAISE EXCEPTION 'module writer groups must not create or own schema objects';
        END IF;
        IF has_database_privilege(r.oid, current_database(), 'TEMP') THEN
            RAISE EXCEPTION 'module writer groups must not create temporary objects (including PUBLIC grants)';
        END IF;
        -- The groups are NOLOGIN: the session belongs to a member login (or to
        -- a role it inherits), so TEMP granted to a member defeats the barrier
        -- just as well. has_database_privilege on the group alone cannot see
        -- it (#919 N1). Walk the explicit memberships in pg_auth_members
        -- instead of pg_has_role: a superuser, like the migration owner
        -- running this audit, is implicitly a member of every role. This
        -- barrier protects the module guards (ameesh_module_requirements,
        -- ameesh_module_milestone_guard), which are ordinary functions and
        -- resolve their tables through the caller's search_path, where pg_temp
        -- comes first.
        IF EXISTS (
            WITH RECURSIVE members(oid) AS (
                SELECT m.member FROM pg_auth_members m WHERE m.roleid = r.oid
                UNION
                SELECT m.member FROM pg_auth_members m JOIN members x ON m.roleid = x.oid
            )
            -- Check every parent conservatively, even when NOINHERIT means
            -- its TEMP becomes usable only after SET ROLE. A login can create
            -- a shadow table there and keep it when switching to the writer.
            SELECT 1 FROM members JOIN pg_roles mr ON mr.oid = members.oid
              JOIN pg_roles capability ON pg_has_role(mr.oid, capability.oid, 'MEMBER')
             WHERE has_database_privilege(capability.oid, current_database(), 'TEMP')
        ) THEN
            RAISE EXCEPTION 'module writer group % has a member with TEMP (direct or inherited)', r.rolname;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles p WHERE p.oid <> r.oid AND pg_has_role(r.oid, p.oid, 'MEMBER')) THEN
            RAISE EXCEPTION 'module writer groups must not inherit another role';
        END IF;
    END LOOP;
    IF has_table_privilege('ameesh_it_proof', 'project_modules', 'INSERT,UPDATE,DELETE') THEN
        RAISE EXCEPTION 'proof writer must not change module policies, including PUBLIC grants';
    END IF;
    IF has_table_privilege('ameesh_it_qa', 'project_modules', 'INSERT,UPDATE,DELETE') THEN
        RAISE EXCEPTION 'QA writer must not change module policies, including PUBLIC grants';
    END IF;
    IF to_regclass('schema_non_additive_migrations') IS NOT NULL AND EXISTS (
        SELECT 1 FROM pg_roles writer_group
         WHERE writer_group.rolname IN ('ameesh_canon_sync', 'ameesh_it_proof', 'ameesh_it_qa')
           AND has_table_privilege(writer_group.oid, 'schema_non_additive_migrations', 'INSERT,UPDATE,DELETE')
    ) THEN
        RAISE EXCEPTION 'module writers must not change the schema compatibility inventory';
    END IF;
    IF to_regclass('schema_migrations') IS NOT NULL AND EXISTS (
        SELECT 1 FROM pg_roles writer_group
         WHERE writer_group.rolname IN ('ameesh_canon_sync', 'ameesh_it_proof', 'ameesh_it_qa')
           AND (has_table_privilege(writer_group.oid, 'schema_migrations', 'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR has_any_column_privilege(writer_group.oid, 'schema_migrations', 'UPDATE'))
    ) THEN
        RAISE EXCEPTION 'module writers must not modify the schema migration inventory';
    END IF;
    -- #2274: the catch-up bound is owner-only; a write left to canon sync
    -- would let the sync service move the review horizon.
    IF to_regclass(format('%I.%I', current_schema(), 'review_catch_up')) IS NOT NULL
       AND has_table_privilege('ameesh_canon_sync',
            to_regclass(format('%I.%I', current_schema(), 'review_catch_up')),
            'INSERT,UPDATE,DELETE') THEN
        RAISE EXCEPTION 'canon sync must not change the review catch-up activation bound';
    END IF;
END
$audit$;
COMMIT;
