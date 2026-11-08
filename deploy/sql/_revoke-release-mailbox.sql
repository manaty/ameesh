-- SPDX-License-Identifier: AGPL-3.0-only
-- Non-additive: controller stopped.
DO $install$
DECLARE
    s text := current_schema();
    r text := current_setting('ameesh.release_role');
    cols text;
BEGIN
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
