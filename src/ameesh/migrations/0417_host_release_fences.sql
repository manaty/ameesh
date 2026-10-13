-- SPDX-License-Identifier: AGPL-3.0-only
-- L232b: release fences are independent of placement withdrawals/handoffs.
CREATE TABLE host_release_operations (
    operation text PRIMARY KEY,
    host text NOT NULL,
    manifest_digest text NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE host_release_fences (
    host text PRIMARY KEY,
    operation text NOT NULL UNIQUE REFERENCES host_release_operations(operation),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE host_release_fence_receipts (
    action_id text PRIMARY KEY,
    operation text NOT NULL REFERENCES host_release_operations(operation),
    step text NOT NULL CHECK (step IN ('fence','unfence','recovery_fence','recovery_unfence')),
    receipt jsonb NOT NULL,
    completed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE OR REPLACE FUNCTION ameesh_host_claims_open(wanted_host text) RETURNS boolean
LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    RETURN NOT EXISTS (SELECT 1 FROM host_lifecycle
                       WHERE host=wanted_host AND mode IN ('draining','lost'))
       AND NOT EXISTS (SELECT 1 FROM host_release_fences WHERE host=wanted_host);
END $$;
-- Default executor grants must not confer release-controller writes.
-- Leave SELECT available to the invoker-rights claim predicate, revoke every
-- non-owner writer present at creation (including global/schema defaults).
DO $release_acl$
DECLARE item record;
BEGIN
    FOR item IN SELECT DISTINCT c.relname,acl.grantee
                  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                  CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) acl
                 WHERE n.nspname=current_schema()
                   AND c.relname IN ('host_release_operations','host_release_fences','host_release_fence_receipts')
                   AND acl.grantee<>c.relowner LOOP
        EXECUTE format('REVOKE INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN ON %I.%I FROM %s',
                       current_schema(),item.relname,
                       CASE WHEN item.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(item.grantee)) END);
    END LOOP;
END
$release_acl$;
