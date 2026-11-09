-- SPDX-License-Identifier: AGPL-3.0-only
-- #6372: executable privacy contract, no dated inventory or mail content.
-- Run as schema owner after role installation and in CI disposable schemas.
\set ON_ERROR_STOP on
\if :{?mailbox_readers}
\else
  \set mailbox_readers '["ameesh","ameesh_host_agents1","auditeur_lecture"]'
\endif
SELECT set_config('ameesh.mailbox_readers', :'mailbox_readers', false) \g /dev/null
DO $audit$
DECLARE
    allowed jsonb := current_setting('ameesh.mailbox_readers')::jsonb;
    mailbox regclass := format('%I.agent_mailbox',current_schema())::regclass;
    offenders text;
BEGIN
    -- MEMBER deliberately covers NOINHERIT/SET ROLE as well as effective
    -- grants; has_column_privilege also includes table grants and PUBLIC.
    SELECT string_agg(DISTINCT login.rolname,', ' ORDER BY login.rolname)
      INTO offenders
      FROM pg_roles login
     WHERE login.rolcanlogin AND NOT login.rolsuper
       AND NOT allowed ? login.rolname
       AND EXISTS (
         SELECT FROM pg_roles reachable
          WHERE (reachable.oid=login.oid OR pg_has_role(login.oid,reachable.oid,'MEMBER'))
            AND (has_table_privilege(reachable.oid,mailbox,'SELECT') OR EXISTS (
              SELECT FROM pg_attribute a WHERE a.attrelid=mailbox
                AND a.attnum>0 AND NOT a.attisdropped
                AND has_column_privilege(reachable.oid,mailbox,a.attnum,'SELECT'))));
    IF offenders IS NOT NULL THEN
        RAISE EXCEPTION 'unexpected mailbox LOGIN reader(s): %',offenders;
    END IF;
    IF EXISTS (SELECT FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) acl
          WHERE c.oid=mailbox AND acl.grantee=0 AND acl.privilege_type='SELECT')
       OR EXISTS (SELECT FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) acl
          WHERE a.attrelid=mailbox AND acl.grantee=0 AND acl.privilege_type='SELECT') THEN
        RAISE EXCEPTION 'PUBLIC mailbox SELECT';
    END IF;
    RAISE NOTICE 'mailbox LOGIN reader contract held';
END
$audit$;
