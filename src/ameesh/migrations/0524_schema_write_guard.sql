-- SPDX-License-Identifier: AGPL-3.0-only
-- #4975: deploy this additive marker and its reader BEFORE the next
-- non-additive migration. Code predating this guard still needs stopping.
CREATE TABLE schema_non_additive_migrations (
    version integer PRIMARY KEY REFERENCES schema_migrations(version),
    name text NOT NULL,
    checksum text NOT NULL
);

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
END $writer_roles$;
