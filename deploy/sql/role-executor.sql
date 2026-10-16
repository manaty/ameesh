-- SPDX-License-Identifier: AGPL-3.0-only
-- L56: executor data access, without schema or database administration.
-- Apply as the migration owner, with the mesh schema first in search_path:
--   PGOPTIONS='-c search_path=<schema>' psql '<owner DSN>' -X \
--     -v ON_ERROR_STOP=1 [-v role=ameesh_executor] -f deploy/sql/role-executor.sql
-- NOLOGIN group role; each host uses a separate LOGIN member.
-- Reapply after migrations. This transaction fails closed on effective
-- privileges, including PUBLIC and parent roles; it never repairs their ACLs.
-- The owner must separately revoke PUBLIC TEMP on this database before use.
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role ameesh_executor
\endif
BEGIN;
SELECT current_schema() AS schema \gset
SELECT set_config('ameesh.role', :'role', true), set_config('ameesh.schema', :'schema', true) \g /dev/null

DO $owner$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace
                    WHERE nspname = current_setting('ameesh.schema')
                      AND pg_has_role(current_user, nspowner, 'USAGE')) THEN
        RAISE EXCEPTION 'Apply role-executor.sql as the migration owner with schema ownership rights';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = current_setting('ameesh.schema')
                      AND c.relname = 'agent_registry' AND c.relowner = current_user::regrole) THEN
        RAISE EXCEPTION 'Apply role-executor.sql as the migration owner (agent_registry owner)';
    END IF;
END
$owner$;

SELECT format('CREATE ROLE %I NOLOGIN', :'role')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
SELECT format('GRANT USAGE ON SCHEMA %I TO %I', :'schema', :'role') \gexec
SELECT format('REVOKE CREATE ON SCHEMA %I FROM %I', :'schema', :'role') \gexec
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I',
              :'schema', :'role') \gexec
SELECT format('GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA %I TO %I',
              :'schema', :'role') \gexec
SELECT format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO %I', :'schema', :'role') \gexec
-- L244 (#420): ameesh_module_agent is SECURITY DEFINER (it takes the shared
-- lock on the registry for the isolated module roles). The executor group is
-- granted EXECUTE by the blanket line above but must never hold it: the audit
-- below flags any executable SECURITY DEFINER function, and rightly so.
SELECT format('REVOKE EXECUTE ON FUNCTION %I.ameesh_module_agent(text) FROM %I',
              :'schema', :'role')
 WHERE EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE n.nspname = :'schema' AND p.proname = 'ameesh_module_agent'
                 AND p.prosecdef) \gexec
-- Module policy writes belong exclusively to isolated canon synchronization.
SELECT format('REVOKE INSERT, UPDATE, DELETE ON %I.project_modules FROM %I',
              :'schema', :'role') WHERE to_regclass(format('%I.project_modules', :'schema')) IS NOT NULL \gexec
-- Only the independently enrolled release controller may write these fences.
SELECT format('REVOKE INSERT, UPDATE, DELETE ON %I.%I FROM %I', :'schema', name, :'role')
  FROM unnest(ARRAY['host_release_operations','host_release_fences','host_release_fence_receipts']) name
 WHERE to_regclass(format('%I.%I', :'schema', name)) IS NOT NULL \gexec
-- #326 r4: the key journal's order is the server's; no setval on its ids
SELECT format('REVOKE UPDATE ON SEQUENCE %I.key_events_id_seq FROM %I', :'schema', :'role')
 WHERE to_regclass(format('%I.key_events_id_seq', :'schema')) IS NOT NULL \gexec
-- Future migration objects must be created by this same owner.
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE '
              'ON TABLES TO %I', :'schema', :'role') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT USAGE, SELECT, UPDATE '
              'ON SEQUENCES TO %I', :'schema', :'role') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT EXECUTE ON FUNCTIONS TO %I',
              :'schema', :'role') \gexec

DO $audit$
DECLARE
    r text := current_setting('ameesh.role');
    s text := current_setting('ameesh.schema');
    violations text[] := '{}';
    parent record;
    item record;
    privilege text;
    missing text[];
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r AND rolcanlogin) THEN
        violations := array_append(violations, 'executor group must be NOLOGIN');
    END IF;
    -- MEMBER also catches NOINHERIT paths: SET ROLE must not evade the audit.
    FOR parent IN SELECT * FROM pg_roles WHERE pg_has_role(r, oid, 'MEMBER') LOOP
        -- Predefined roles carry capabilities without administrative attributes.
        IF parent.rolname IN ('ameesh_it_proof', 'ameesh_canon_sync', 'pg_read_server_files', 'pg_write_server_files',
                             'pg_execute_server_program', 'pg_signal_backend',
                             'pg_checkpoint', 'pg_maintain', 'pg_create_subscription',
                             'pg_read_all_data', 'pg_write_all_data') THEN
            violations := violations || format('privileged predefined role %s', parent.rolname);
        END IF;
        IF parent.rolsuper OR parent.rolcreatedb OR parent.rolcreaterole
           OR parent.rolbypassrls OR parent.rolreplication THEN
            violations := violations || format('administrative attributes on role %s', parent.rolname);
        END IF;
        FOR item IN SELECT oid, nspname, nspowner FROM pg_namespace
                     WHERE nspname !~ '^pg_temp_' AND nspname !~ '^pg_toast_temp_' LOOP
            IF has_schema_privilege(parent.oid, item.oid, 'CREATE')
               OR pg_has_role(parent.oid, item.nspowner, 'MEMBER') THEN
                violations := violations || format('CREATE or ownership on schema %s through %s',
                                                   item.nspname, parent.rolname);
            END IF;
        END LOOP;
        FOR item IN SELECT oid, datname FROM pg_database WHERE datallowconn LOOP
            IF has_database_privilege(parent.oid, item.oid, 'CREATE') THEN
                violations := violations || format('CREATE on database %s through %s',
                                                   item.datname, parent.rolname);
            END IF;
        END LOOP;
        IF has_database_privilege(parent.oid, current_database(), 'TEMP') THEN
            violations := violations || format('TEMP on database %s through %s (including PUBLIC)',
                                               current_database(), parent.rolname);
        END IF;
        FOR item IN SELECT c.oid, c.relname FROM pg_class c
                     JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = s AND c.relkind IN ('r', 'p', 'v', 'm', 'f') LOOP
            FOREACH privilege IN ARRAY ARRAY['TRIGGER', 'TRUNCATE', 'REFERENCES', 'MAINTAIN'] LOOP
                IF has_table_privilege(parent.oid, item.oid, privilege) THEN
                    violations := violations || format('%s on table %s through %s',
                                                       privilege, item.relname, parent.rolname);
                END IF;
            END LOOP;
        END LOOP;
        IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = s AND pg_has_role(parent.oid, c.relowner, 'MEMBER'))
           OR EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                       WHERE n.nspname = s AND pg_has_role(parent.oid, p.proowner, 'MEMBER')) THEN
            violations := violations || format('mesh object ownership through %s', parent.rolname);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = s AND p.prosecdef AND has_function_privilege(r, p.oid, 'EXECUTE')) THEN
        violations := array_append(violations, 'executable SECURITY DEFINER function in mesh schema');
    END IF;
    -- Both global and schema defaults apply to future objects. Check every
    -- creator able to create in the mesh, including PUBLIC and SET ROLE paths.
    FOR item IN SELECT d.defaclrole, acl.privilege_type FROM pg_default_acl d
                CROSS JOIN LATERAL aclexplode(d.defaclacl) acl
                WHERE d.defaclobjtype = 'r'
                  AND d.defaclnamespace IN (0, (SELECT oid FROM pg_namespace WHERE nspname = s))
                  AND has_schema_privilege(d.defaclrole, s, 'CREATE')
                  AND acl.privilege_type IN ('TRIGGER', 'TRUNCATE', 'REFERENCES', 'MAINTAIN')
                  AND (acl.grantee = 0 OR EXISTS (SELECT 1 FROM pg_roles
                       WHERE oid = acl.grantee AND pg_has_role(r, oid, 'MEMBER'))) LOOP
        violations := violations || format('unsafe default %s for creator %s',
                                           item.privilege_type, pg_get_userbyid(item.defaclrole));
    END LOOP;
    -- Comma-separated privilege strings mean ANY, so test every data right
    -- separately for every relation; GRANT can warn and leave objects missing.
    FOR item IN SELECT c.oid, c.relname, c.relkind FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = s AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') LOOP
        IF item.relkind = 'S' THEN
            FOREACH privilege IN ARRAY ARRAY['USAGE', 'SELECT', 'UPDATE'] LOOP
                IF item.relname = 'key_events_id_seq' AND privilege = 'UPDATE' THEN
                    IF has_sequence_privilege(r, item.oid, privilege) THEN
                        violations := violations || format('UPDATE (setval) on sequence %s', item.relname);
                    END IF;
                ELSIF NOT has_sequence_privilege(r, item.oid, privilege) THEN
                    violations := violations || format('missing %s on sequence %s', privilege, item.relname);
                END IF;
            END LOOP;
        ELSIF item.relname IN ('project_modules','host_release_operations','host_release_fences','host_release_fence_receipts') THEN
            IF NOT has_table_privilege(r, item.oid, 'SELECT') THEN
                violations := violations || format('missing SELECT on %s', item.relname);
            END IF;
            FOREACH privilege IN ARRAY ARRAY['INSERT', 'UPDATE', 'DELETE'] LOOP
                IF has_table_privilege(r, item.oid, privilege) THEN
                    violations := violations || format('forbidden %s on %s', privilege, item.relname);
                END IF;
            END LOOP;
        ELSE
            FOREACH privilege IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'] LOOP
                IF NOT has_table_privilege(r, item.oid, privilege) THEN
                    violations := violations || format('missing %s on table %s', privilege, item.relname);
                END IF;
            END LOOP;
        END IF;
    END LOOP;
    IF cardinality(violations) > 0 THEN
        RAISE EXCEPTION 'ROLE CONTRACT FAILED for %: %', r, array_to_string(violations, '; ');
    END IF;
    -- Not a violation: the event stream's cluster identity (0396). Executable
    -- by PUBLIC unless the server revoked them; then every resume is a reset.
    SELECT array_agg(f ORDER BY f) INTO missing FROM unnest(ARRAY[
        'pg_control_system()', 'pg_control_checkpoint()', 'pg_current_wal_lsn()',
        'pg_walfile_name(pg_lsn)']) f
     WHERE NOT has_function_privilege(r, f::regprocedure, 'EXECUTE');
    IF missing IS NOT NULL THEN
        RAISE WARNING 'role % may not execute %: event stream resumes will all reset; '
                      'as a superuser: GRANT EXECUTE ON FUNCTION % TO %',
                      r, array_to_string(missing, ', '), array_to_string(missing, ', '),
                      quote_ident(r);
    END IF;
END
$audit$;
COMMIT;
