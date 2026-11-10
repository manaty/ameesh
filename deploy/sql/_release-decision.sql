-- SPDX-License-Identifier: AGPL-3.0-only
-- #6372: additive endpoint inside caller transaction; preserves controller SELECT.
-- #6377: the isolated SECURITY DEFINER owner is no longer named
-- `<controller>_decision_reader`, which migration 0527's controller prefix
-- (`ameesh_release_controller[_%]`) would enroll as a controller. The owner
-- name is derived from a digest of the controller role, outside every
-- controller prefix. An installation that already created the legacy role is
-- migrated in place by ALTER ROLE ... RENAME: function ownership and ACLs
-- follow the role OID, so no endpoint is dropped or re-owned by name.
DO $install$
DECLARE
    s text := current_schema();
    r text := current_setting('ameesh.release_role');
    w text := 'ameesh_decision_owner_' || substr(md5(current_setting('ameesh.release_role')),1,12);
    legacy text := current_setting('ameesh.release_role') || '_decision_reader';
    mark text := 'ameesh-decision-owner:' || current_setting('ameesh.release_role');
    renamed boolean := false;
    acl record;
    fn text;
BEGIN
    IF legacy <> w AND EXISTS (SELECT FROM pg_roles WHERE rolname=legacy) THEN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname=w) THEN
            RAISE EXCEPTION 'both legacy and current decision owners exist; reconcile before reinstall';
        END IF;
        -- Rename only the definer of THIS schema's endpoints: a same-named
        -- foreign role must not be adopted silently.
        IF NOT EXISTS (
            SELECT FROM pg_proc p JOIN pg_roles o ON o.oid=p.proowner
             WHERE o.rolname=legacy
               AND p.pronamespace=(SELECT oid FROM pg_namespace WHERE nspname=s)
               AND p.proname IN ('release_controller_decision','release_controller_decision_ids')) THEN
            RAISE EXCEPTION 'legacy decision owner role is not this installation''s definer';
        END IF;
        EXECUTE format('ALTER ROLE %I RENAME TO %I',legacy,w);
        renamed := true;
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname=w) AND NOT renamed
       AND shobj_description((SELECT oid FROM pg_roles WHERE rolname=w),'pg_authid')
           IS DISTINCT FROM mark THEN
        RAISE EXCEPTION 'decision owner role exists without this controller''s marker';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname=w) THEN
        EXECUTE format('CREATE ROLE %I NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',w);
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname=w AND
        (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls))
       OR EXISTS (SELECT FROM pg_auth_members WHERE roleid=(SELECT oid FROM pg_roles WHERE rolname=w)
          OR member=(SELECT oid FROM pg_roles WHERE rolname=w)) THEN
        RAISE EXCEPTION 'decision reader must be isolated nonadministrative NOLOGIN';
    END IF;
    IF has_schema_privilege(w,s,'CREATE') THEN
        RAISE EXCEPTION 'unexpected decision owner CREATE';
    END IF;
    EXECUTE format('COMMENT ON ROLE %I IS %L',w,mark);
    -- Migration 0527 matched the legacy name as a controller and granted it
    -- SELECT on decision_supersessions. The definer never reads that log.
    IF to_regclass(format('%I.decision_supersessions',s)) IS NOT NULL THEN
        EXECUTE format('REVOKE SELECT ON %I.decision_supersessions FROM %I',s,w);
    END IF;
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I',s,w);
    EXECUTE format('GRANT SELECT (id,kind,sender,recipient,payload,host,created_at,delivered_at) ON %I.agent_mailbox TO %I',s,w);
    EXECUTE format($ddl$
CREATE OR REPLACE FUNCTION %1$I.release_controller_decision(p_id bigint)
RETURNS TABLE(id bigint,sender text,recipient text,payload jsonb,host text,created_ts double precision,closed_ts double precision)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp
AS $fn$
    SELECT m.id::bigint,m.sender::text,m.recipient::text,
           jsonb_build_object('decision',m.payload->'decision'),m.host::text,
           extract(epoch FROM m.created_at)::float8,
           extract(epoch FROM m.delivered_at)::float8
      FROM %1$I.agent_mailbox m
     WHERE m.id=p_id AND m.kind='request' AND m.recipient LIKE 'human:%%'
       AND m.payload ? 'decision'
       AND m.payload->'decision'->'needs_signature' = 'true'::jsonb
$fn$;
$ddl$,s);
    EXECUTE format($ddl$
CREATE OR REPLACE FUNCTION %1$I.release_controller_decision_ids(p_question text)
RETURNS TABLE(id bigint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp
AS $fn$
    SELECT m.id::bigint FROM %1$I.agent_mailbox m
     WHERE m.kind='request' AND m.recipient LIKE 'human:%%'
       AND m.payload->'decision'->'needs_signature' = 'true'::jsonb
       AND m.payload->'decision'->>'question'=p_question
       AND m.payload->'decision'->>'state'='answered'
$fn$;
$ddl$,s);
    FOREACH fn IN ARRAY ARRAY['release_controller_decision(bigint)',
                             'release_controller_decision_ids(text)'] LOOP
        -- Default privileges may grant EXECUTE to executor groups. Close every
        -- explicit grant before exposing this endpoint only to its controller.
        FOR acl IN SELECT DISTINCT x.grantee FROM pg_proc p
            CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) x
            WHERE p.oid=format('%I.%s',s,fn)::regprocedure
              AND x.grantee<>p.proowner AND x.grantee<>0 LOOP
            EXECUTE format('REVOKE ALL ON FUNCTION %I.%s FROM %I',
                           s,fn,pg_get_userbyid(acl.grantee));
        END LOOP;
        EXECUTE format('REVOKE ALL ON FUNCTION %I.%s FROM PUBLIC',s,fn);
        EXECUTE format('GRANT CREATE ON SCHEMA %I TO %I',s,w);
        EXECUTE format('ALTER FUNCTION %I.%s OWNER TO %I',s,fn,w);
        EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM %I',s,w);
        EXECUTE format('GRANT EXECUTE ON FUNCTION %I.%s TO %I',s,fn,r);
    END LOOP;
END
$install$;
