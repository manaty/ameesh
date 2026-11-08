-- SPDX-License-Identifier: AGPL-3.0-only
-- #6372: included inside role-release-controller.sql's transaction.
-- Non-additive: bureau installation outside release windows, never migration.
DO $install$
DECLARE
    s text := current_schema();
    r text := current_setting('ameesh.release_role');
    w text := current_setting('ameesh.release_role') || '_decision_reader';
    cols text;
    acl record;
BEGIN
    IF octet_length(w)>63 THEN RAISE EXCEPTION 'controller role name too long'; END IF;
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
    -- Default privileges may grant EXECUTE to executor groups. Close every
    -- explicit grant before exposing this endpoint only to its controller.
    FOR acl IN SELECT DISTINCT x.grantee FROM pg_proc p
        CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) x
        WHERE p.oid=format('%I.release_controller_decision(bigint)',s)::regprocedure
          AND x.grantee<>p.proowner AND x.grantee<>0 LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %I.release_controller_decision(bigint) FROM %I',
                       s,pg_get_userbyid(acl.grantee));
    END LOOP;
    EXECUTE format('REVOKE ALL ON FUNCTION %I.release_controller_decision(bigint) FROM PUBLIC',s);
    EXECUTE format('GRANT CREATE ON SCHEMA %I TO %I',s,w);
    EXECUTE format('ALTER FUNCTION %I.release_controller_decision(bigint) OWNER TO %I',s,w);
    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM %I',s,w);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %I.release_controller_decision(bigint) TO %I',s,r);
    SELECT string_agg(quote_ident(attname),',') INTO cols FROM pg_attribute
      WHERE attrelid=format('%I.agent_mailbox',s)::regclass AND attnum>0 AND NOT attisdropped;
    EXECUTE format('REVOKE SELECT ON %I.agent_mailbox FROM %I',s,r);
    EXECUTE format('REVOKE SELECT (%s) ON %I.agent_mailbox FROM %I',cols,s,r);
    IF EXISTS (SELECT FROM pg_roles usable WHERE (usable.rolname=r OR pg_has_role(r,usable.oid,'MEMBER'))
       AND EXISTS (SELECT FROM pg_attribute a WHERE a.attrelid=format('%I.agent_mailbox',s)::regclass
           AND a.attnum>0 AND NOT a.attisdropped
           AND has_column_privilege(usable.oid,a.attrelid,a.attnum,'SELECT'))) THEN
        RAISE EXCEPTION 'controller still reads mailbox directly or via SET ROLE';
    END IF;
END
$install$;
