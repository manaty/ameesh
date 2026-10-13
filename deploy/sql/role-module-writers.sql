-- SPDX-License-Identifier: AGPL-3.0-only
-- Apply after migration 0363, as the schema/migration owner.
-- These NOLOGIN groups belong to separate protected service logins, NEVER
-- to executor logins. Credentials and canon/code are isolated at the OS level.
\set ON_ERROR_STOP on
BEGIN;
SELECT current_schema() AS schema \gset
SELECT 'CREATE ROLE ameesh_canon_sync NOLOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ameesh_canon_sync') \gexec
SELECT 'CREATE ROLE ameesh_it_proof NOLOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ameesh_it_proof') \gexec
SELECT format('GRANT USAGE ON SCHEMA %I TO ameesh_canon_sync, ameesh_it_proof', :'schema') \gexec
-- Canon sync executes the existing registry/authenticator/package transaction.
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO ameesh_canon_sync', :'schema') \gexec
SELECT format('REVOKE INSERT, UPDATE, DELETE ON %I.%I FROM ameesh_canon_sync', :'schema', name)
  FROM unnest(ARRAY['host_release_operations','host_release_fences','host_release_fence_receipts']) name
 WHERE to_regclass(format('%I.%I', :'schema', name)) IS NOT NULL \gexec
SELECT format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO ameesh_canon_sync', :'schema') \gexec
SELECT format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO ameesh_canon_sync, ameesh_it_proof', :'schema') \gexec
SELECT format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM ameesh_it_proof', :'schema') \gexec
SELECT format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM ameesh_it_proof', :'schema') \gexec
SELECT format('GRANT SELECT ON %I.work_items, %I.project_modules, %I.work_module_milestones TO ameesh_it_proof',
              :'schema', :'schema', :'schema') \gexec
-- SELECT FOR NO KEY UPDATE needs UPDATE on at least one column.
SELECT format('GRANT UPDATE (id) ON %I.work_items TO ameesh_it_proof', :'schema') \gexec
SELECT format('GRANT INSERT ON %I.work_module_milestones TO ameesh_it_proof', :'schema') \gexec
SELECT format('GRANT USAGE ON SEQUENCE %I.work_module_milestones_id_seq TO ameesh_it_proof', :'schema') \gexec
DO $audit$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM pg_roles WHERE rolname IN ('ameesh_it_proof', 'ameesh_canon_sync') LOOP
        IF r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolbypassrls OR r.rolreplication OR r.rolcanlogin THEN
            RAISE EXCEPTION 'module writer groups must be nonadministrative NOLOGIN roles';
        END IF;
        IF has_schema_privilege(r.oid, current_schema(), 'CREATE') THEN
            RAISE EXCEPTION 'module writer groups must not create or own schema objects';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles p WHERE p.oid <> r.oid AND pg_has_role(r.oid, p.oid, 'MEMBER')) THEN
            RAISE EXCEPTION 'module writer groups must not inherit another role';
        END IF;
    END LOOP;
    IF has_table_privilege('ameesh_it_proof', 'project_modules', 'INSERT,UPDATE,DELETE') THEN
        RAISE EXCEPTION 'proof writer must not change module policies, including PUBLIC grants';
    END IF;
END
$audit$;
COMMIT;
