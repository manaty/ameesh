-- SPDX-License-Identifier: AGPL-3.0-only
-- #6372 / #6377: executable privacy contract, no dated inventory or mail content.
-- Run as schema owner after role installation and in CI disposable schemas.
--
-- Readers are admitted by their contract, never by a deployment host name:
--   * the mailbox relation owner (ownership already carries SELECT);
--   * members of the executor group (role-executor.sql grants that group
--     SELECT on every mesh table, so its logins are readers by construction);
--   * the backup identity enrolled by enroler-identites.py, recognised by the
--     role comment that the enrollment writes (kind "backup");
--   * the explicit read-only logins listed in `mailbox_readers`
--     (default: the canonical database login and the auditor contract login).
-- Any other LOGIN with an effective SELECT, direct or through SET ROLE, fails.
\set ON_ERROR_STOP on
\if :{?mailbox_readers}
\else
  \set mailbox_readers '["ameesh","auditeur_lecture"]'
\endif
\if :{?mailbox_executor}
\else
  \set mailbox_executor 'ameesh_executor'
\endif
SELECT set_config('ameesh.mailbox_readers', :'mailbox_readers', false),
       set_config('ameesh.mailbox_executor', :'mailbox_executor', false) \g /dev/null
DO $audit$
DECLARE
    allowed jsonb := current_setting('ameesh.mailbox_readers')::jsonb;
    executor_oid oid := (SELECT oid FROM pg_roles
                          WHERE rolname=current_setting('ameesh.mailbox_executor'));
    mailbox regclass := format('%I.agent_mailbox',current_schema())::regclass;
    mailbox_owner oid := (SELECT relowner FROM pg_class WHERE oid=mailbox);
    offenders text;
BEGIN
    -- MEMBER deliberately covers NOINHERIT/SET ROLE as well as effective
    -- grants; has_column_privilege also includes table grants and PUBLIC.
    -- pg_has_role(_, NULL, ...) is NULL: an absent executor group admits none.
    SELECT string_agg(DISTINCT login.rolname,', ' ORDER BY login.rolname)
      INTO offenders
      FROM pg_roles login
     WHERE login.rolcanlogin AND NOT login.rolsuper
       AND login.oid <> mailbox_owner
       AND NOT coalesce(pg_has_role(login.oid,executor_oid,'MEMBER'),false)
       AND NOT allowed ? login.rolname
       AND NOT EXISTS (
         SELECT FROM pg_roles enrolled
          WHERE enrolled.oid=login.oid
            AND shobj_description(enrolled.oid,'pg_authid')
                ~ '^ameesh-release-identities:[a-z][a-z0-9_]{0,62}:backup$')
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
