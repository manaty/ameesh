-- SPDX-License-Identifier: AGPL-3.0-only
-- L56: executor data access, without schema or database administration.
-- Apply as the schema owner, with the mesh schema first in search_path:
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
                      AND nspowner = current_user::regrole) THEN
        RAISE EXCEPTION 'Apply role-executor.sql as the schema owner';
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
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r AND rolcanlogin) THEN
        violations := array_append(violations, 'executor group must be NOLOGIN');
    END IF;
    -- MEMBER also catches NOINHERIT paths: SET ROLE must not evade the audit.
    FOR parent IN SELECT * FROM pg_roles WHERE pg_has_role(r, oid, 'MEMBER') LOOP
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
    -- Comma-separated privilege strings mean ANY, so test every data right.
    FOR item IN SELECT unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) AS privilege LOOP
        IF NOT has_table_privilege(r, format('%I.agent_registry', s), item.privilege) THEN
            violations := violations || format('missing %s on agent_registry', item.privilege);
        END IF;
    END LOOP;
    IF cardinality(violations) > 0 THEN
        RAISE EXCEPTION 'ROLE CONTRACT FAILED for %: %', r, array_to_string(violations, '; ');
    END IF;
END
$audit$;
COMMIT;
