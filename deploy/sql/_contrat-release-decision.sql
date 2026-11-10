-- SPDX-License-Identifier: AGPL-3.0-only
-- #6372: controller endpoint must stay private and unavailable via SET ROLE.
-- #6377: the definer owner is derived outside the controller prefix of
-- migration 0527. A legacy `<controller>_decision_reader` is refused here:
-- release-decision-additive.sql renames it in place, and this audit never
-- adopts a role by name alone (the enrollment marker names its controller).
DO $audit$
DECLARE
    s text := current_schema();
    r text := current_setting('ameesh.release_role');
    w text := 'ameesh_decision_owner_' || substr(md5(r),1,12);
    legacy text := r || '_decision_reader';
    mark text := 'ameesh-decision-owner:' || r;
    fn regprocedure;
    spec record;
BEGIN
    IF legacy <> w AND EXISTS (SELECT FROM pg_roles WHERE rolname=legacy) THEN
        RAISE EXCEPTION 'legacy decision owner % present: re-run release-decision-additive.sql from the current head',legacy;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname=w
                   AND shobj_description(oid,'pg_authid')=mark) THEN
        RAISE EXCEPTION 'decision owner marker missing or foreign';
    END IF;
    IF EXISTS (SELECT FROM pg_roles login JOIN pg_roles reachable
          ON reachable.oid=login.oid OR pg_has_role(login.oid,reachable.oid,'MEMBER')
        WHERE NOT login.rolsuper AND (login.rolname=r OR pg_has_role(login.oid,(SELECT oid FROM pg_roles WHERE rolname=r),'MEMBER'))
          AND EXISTS (SELECT FROM pg_attribute a WHERE a.attrelid=format('%I.agent_mailbox',s)::regclass
            AND a.attnum>0 AND NOT a.attisdropped
            AND has_column_privilege(reachable.oid,a.attrelid,a.attnum,'SELECT'))) THEN
        RAISE EXCEPTION 'controller or member can read mailbox directly / SET ROLE';
    END IF;
    FOR spec IN SELECT * FROM (VALUES
        ('release_controller_decision(bigint)', 'ba2cbab33329c99db11db90440f01150'),
        ('release_controller_decision_ids(text)', '4248663103d767cc5b3edf471ed819cc')
    ) AS endpoints(signature,digest) LOOP
        fn := format('%I.%s',s,spec.signature)::regprocedure;
        IF NOT EXISTS (SELECT FROM pg_proc WHERE oid=fn AND prosecdef AND provolatile='s'
            AND NOT proleakproof AND proowner=(SELECT oid FROM pg_roles WHERE rolname=w)
            AND proconfig=ARRAY['search_path=pg_catalog, pg_temp']
            AND md5(replace(prosrc,quote_ident(s)||'.','<mesh>.'))=spec.digest) THEN
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
    END LOOP;
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
