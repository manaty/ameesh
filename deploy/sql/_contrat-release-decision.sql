-- SPDX-License-Identifier: AGPL-3.0-only
-- #6372: controller endpoint must stay private and unavailable via SET ROLE.
DO $audit$
DECLARE
    s text := current_schema();
    r text := current_setting('ameesh.release_role');
    w text := r || '_decision_reader';
    fn regprocedure := format('%I.release_controller_decision(bigint)',s)::regprocedure;
BEGIN
    IF EXISTS (SELECT FROM pg_roles login JOIN pg_roles reachable
          ON reachable.oid=login.oid OR pg_has_role(login.oid,reachable.oid,'MEMBER')
        WHERE NOT login.rolsuper AND (login.rolname=r OR pg_has_role(login.oid,(SELECT oid FROM pg_roles WHERE rolname=r),'MEMBER'))
          AND EXISTS (SELECT FROM pg_attribute a WHERE a.attrelid=format('%I.agent_mailbox',s)::regclass
            AND a.attnum>0 AND NOT a.attisdropped
            AND has_column_privilege(reachable.oid,a.attrelid,a.attnum,'SELECT'))) THEN
        RAISE EXCEPTION 'controller or member can read mailbox directly / SET ROLE';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_proc WHERE oid=fn AND prosecdef AND provolatile='s'
        AND NOT proleakproof AND proowner=(SELECT oid FROM pg_roles WHERE rolname=w)
        AND proconfig=ARRAY['search_path=pg_catalog, pg_temp']
        AND md5(replace(prosrc,quote_ident(s)||'.','<mesh>.'))='ba2cbab33329c99db11db90440f01150') THEN
        RAISE EXCEPTION 'invalid release decision definer';
    END IF;
    IF EXISTS (SELECT FROM pg_proc p CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) acl
        WHERE p.oid=fn AND (acl.grantee NOT IN
          (SELECT oid FROM pg_roles WHERE rolname IN (r,w)) OR acl.is_grantable AND acl.grantee<>p.proowner)) THEN
        RAISE EXCEPTION 'release decision EXECUTE exposed outside controller';
    END IF;
    IF NOT has_function_privilege(r,fn,'EXECUTE') THEN
        RAISE EXCEPTION 'controller lacks decision endpoint';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname=w AND
          (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls))
       OR has_schema_privilege(w,s,'CREATE') THEN
        RAISE EXCEPTION 'invalid decision owner privileges';
    END IF;
    IF EXISTS (SELECT FROM pg_auth_members WHERE roleid=(SELECT oid FROM pg_roles WHERE rolname=w)
        OR member=(SELECT oid FROM pg_roles WHERE rolname=w)) THEN
        RAISE EXCEPTION 'decision owner role must remain isolated';
    END IF;
END
$audit$;
