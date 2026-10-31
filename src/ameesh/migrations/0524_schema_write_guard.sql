-- SPDX-License-Identifier: AGPL-3.0-only
-- #4975: deploy this additive marker and its reader BEFORE the next
-- non-additive migration. Code predating this guard still needs stopping.
CREATE TABLE schema_non_additive_migrations (
    version integer PRIMARY KEY REFERENCES schema_migrations(version) ON DELETE CASCADE,
    name text NOT NULL,
    checksum text NOT NULL
);

-- Revoke creation-time defaults and existing writers on the parent inventory.
-- DELETE of a parent would erase the marker by cascade; keep migration INSERT.
DO $marker_acl$
DECLARE item record;
BEGIN
    FOR item IN SELECT DISTINCT c.relname,acl.grantee,acl.privilege_type
                  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                  CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) acl
                 WHERE n.nspname=current_schema()
                   AND c.relname IN ('schema_non_additive_migrations', 'schema_migrations')
                   AND acl.grantee<>c.relowner
                   AND acl.privilege_type<>'SELECT'
                   AND NOT (c.relname='schema_migrations' AND acl.privilege_type='INSERT') LOOP
        EXECUTE format('REVOKE %s ON %I.%I FROM %s',
                       item.privilege_type,
                       current_schema(),item.relname,
                       CASE WHEN item.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(item.grantee)) END);
    END LOOP;
END
$marker_acl$;

-- Isolated services already provisioned in this schema predate this table.
-- Do not attach ACLs merely because a service group exists in another schema.
-- Only the migration owner may change its compatibility inventory.
DO $writer_roles$
DECLARE writer_role text;
BEGIN
    FOR writer_role IN
        SELECT rolname FROM pg_roles
         WHERE rolname IN ('ameesh_canon_sync', 'ameesh_it_proof', 'ameesh_it_qa')
           AND has_table_privilege(oid, to_regclass('project_modules'), 'SELECT')
    LOOP
        EXECUTE format('GRANT SELECT ON schema_non_additive_migrations TO %I', writer_role);
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'ameesh_release_controller'
          AND has_table_privilege(oid, to_regclass('host_release_operations'), 'INSERT')
    ) THEN
        GRANT SELECT ON schema_non_additive_migrations TO ameesh_release_controller;
    END IF;
END $writer_roles$;
