-- SPDX-License-Identifier: AGPL-3.0-only
-- Apply after migration 0363, as the schema/migration owner.
-- These NOLOGIN groups belong to separate protected service logins, NEVER
-- to executor logins. Credentials and canon/code are isolated at the OS level.
-- Revoke PUBLIC TEMP on the database separately before applying this script.
-- The audit rejects effective TEMP, including grants inherited from PUBLIC.
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
SELECT format('REVOKE INSERT, UPDATE, DELETE ON %I.%I FROM ameesh_canon_sync', :'schema', name)
  FROM unnest(ARRAY['host_release_operations','host_release_fences','host_release_fence_receipts']) name
 WHERE to_regclass(format('%I.%I', :'schema', name)) IS NOT NULL \gexec
SELECT format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO ameesh_canon_sync', :'schema') \gexec
SELECT format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO ameesh_canon_sync, ameesh_it_proof, ameesh_it_qa', :'schema') \gexec
SELECT format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM ameesh_it_proof, ameesh_it_qa', :'schema') \gexec
SELECT format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM ameesh_it_proof, ameesh_it_qa', :'schema') \gexec
SELECT format('GRANT SELECT ON %I.work_items, %I.project_modules, %I.work_module_milestones TO ameesh_it_proof, ameesh_it_qa',
              :'schema', :'schema', :'schema') \gexec
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
END
$audit$;
COMMIT;
