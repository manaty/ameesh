-- SPDX-License-Identifier: AGPL-3.0-only
-- L232b: independently enrolled controller; NOT a migration/backup/root role.
-- Apply as the schema owner only as part of an approved enrollment.
-- Protected login credentials belong to the independent service, never runners.
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role ameesh_release_controller
\endif
BEGIN;
SELECT current_schema() AS schema \gset
SELECT set_config('ameesh.release_role', :'role', true) \g /dev/null
SELECT format('CREATE ROLE %I NOLOGIN', :'role')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:'role') \gexec
SELECT format('GRANT USAGE ON SCHEMA %I TO %I', :'schema', :'role') \gexec
-- #4995: the authority gate re-reads the signed replacement log
-- (decisions.require_current); the controller needs SELECT on it, never a
-- write. This script is the (re)enrollment source, and migration 0527 grants
-- the same SELECT to controllers already enrolled.
SELECT format('GRANT SELECT ON %I.agent_registry,%I.agent_mailbox,%I.mesh_approvals,%I.mesh_consumed_nonces,%I.turn_resources,%I.spend_pending,%I.host_release_operations,%I.host_release_fences,%I.host_release_fence_receipts,%I.decision_supersessions TO %I',
              :'schema',:'schema',:'schema',:'schema',:'schema',:'schema',:'schema',:'schema',:'schema',:'schema',:'role') \gexec
SELECT format('GRANT SELECT ON %I.mesh_approvals_status TO %I', :'schema', :'role') \gexec
-- SignedDecisionAuthority re-reads the owner key at every step (verify_proof,
-- key_info); mesh_keys is a view, outside the table-write audit below.
SELECT format('GRANT SELECT ON %I.mesh_keys TO %I', :'schema', :'role') \gexec
-- SELECT FOR UPDATE locks the agent boundary without changing its state.
SELECT format('GRANT UPDATE(updated_at) ON %I.agent_registry TO %I', :'schema', :'role') \gexec
SELECT format('GRANT INSERT,UPDATE,DELETE ON %I.host_release_operations,%I.host_release_fences,%I.host_release_fence_receipts TO %I',
              :'schema',:'schema',:'schema',:'role') \gexec
DO $audit$
DECLARE
    r record;
    item record;
    column_right record;
    p text;
    -- MAINTAIN only exists from PostgreSQL 17: before it, asking
    -- has_table_privilege() for that name raises and would fail the whole
    -- enrollment audit, so the server version builds the list.
    privileges text[] := ARRAY['INSERT','UPDATE','DELETE','TRUNCATE','TRIGGER','REFERENCES'];
    version_num int := current_setting('server_version_num')::int;
BEGIN
    IF version_num >= 170000 THEN
        privileges := array_append(privileges, 'MAINTAIN');
    END IF;
    SELECT * INTO r FROM pg_roles WHERE rolname=current_setting('ameesh.release_role');
    IF r.rolcanlogin OR r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolbypassrls OR r.rolreplication THEN
        RAISE EXCEPTION 'release controller group must be nonadministrative NOLOGIN';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles parent WHERE parent.oid<>r.oid AND pg_has_role(r.oid,parent.oid,'MEMBER')) THEN
        RAISE EXCEPTION 'release controller must not inherit another role';
    END IF;
    IF has_database_privilege(r.oid,current_database(),'CREATE,TEMP') THEN
        RAISE EXCEPTION 'release controller must not create database objects (including PUBLIC)';
    END IF;
    FOR item IN SELECT oid,nspowner FROM pg_namespace LOOP
        IF has_schema_privilege(r.oid,item.oid,'CREATE') OR pg_has_role(r.oid,item.nspowner,'MEMBER') THEN
            RAISE EXCEPTION 'release controller must not create or own schema objects';
        END IF;
    END LOOP;
    FOR item IN SELECT c.oid,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                 WHERE n.nspname=current_schema() AND c.relkind IN ('r','p','v','m','f') LOOP
        FOREACH p IN ARRAY privileges LOOP
            IF item.relname='agent_registry' AND p='UPDATE' THEN
                -- Table UPDATE would allow turn/lease and owner-key changes;
                -- only the metadata timestamp is admitted for row-locking.
                IF has_table_privilege(r.oid,item.oid,p) THEN
                    RAISE EXCEPTION 'release controller has broad registry UPDATE';
                END IF;
            ELSIF item.relname IN ('host_release_operations','host_release_fences','host_release_fence_receipts')
                  AND p IN ('INSERT','UPDATE','DELETE') THEN
                IF NOT has_table_privilege(r.oid,item.oid,p) THEN
                    RAISE EXCEPTION 'release controller lacks its fence write rights';
                END IF;
            ELSIF has_table_privilege(r.oid,item.oid,p) THEN
                RAISE EXCEPTION 'release controller has unrelated write privilege';
            END IF;
        END LOOP;
        IF item.relname NOT IN ('host_release_operations','host_release_fences','host_release_fence_receipts') THEN
            FOR column_right IN SELECT attnum,attname FROM pg_attribute WHERE attrelid=item.oid
                                AND attnum>0 AND NOT attisdropped LOOP
                FOREACH p IN ARRAY ARRAY['INSERT','UPDATE','REFERENCES'] LOOP
                    IF item.relname='agent_registry' AND column_right.attname='updated_at' AND p='UPDATE' THEN
                        CONTINUE;
                    END IF;
                    IF has_column_privilege(r.oid,item.oid,column_right.attnum,p) THEN
                        RAISE EXCEPTION 'release controller has unrelated column write privilege';
                    END IF;
                END LOOP;
            END LOOP;
        END IF;
    END LOOP;
    -- #4995: the authority gate re-reads the signed replacement log; an
    -- enrollment without this read would fail every signed operation.
    IF to_regclass(format('%I.decision_supersessions', current_schema())) IS NOT NULL
       AND NOT has_table_privilege(r.oid,
               format('%I.decision_supersessions', current_schema()), 'SELECT') THEN
        RAISE EXCEPTION 'release controller lacks SELECT on decision_supersessions';
    END IF;
    -- Sequences carry their own privileges, outside relkind r/p: USAGE allows
    -- nextval/currval and UPDATE allows setval, so a grant on a schema sequence
    -- would move the schema's counters. The controller needs none of them.
    FOR item IN SELECT c.oid,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                 WHERE n.nspname=current_schema() AND c.relkind='S' LOOP
        FOREACH p IN ARRAY ARRAY['USAGE','SELECT','UPDATE'] LOOP
            IF has_sequence_privilege(r.oid,item.oid,p) THEN
                RAISE EXCEPTION 'release controller has unrelated sequence privilege % on %', p, item.relname;
            END IF;
        END LOOP;
    END LOOP;
END
$audit$;
COMMIT;
