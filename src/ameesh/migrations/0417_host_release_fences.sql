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
    -- Enrolled probe agent of the host: the only identity a live fence still
    -- admits, so the controller can run its one authenticated health turn on a
    -- drained host. Every other claim stays closed (see ameesh_agent_claims_open).
    probe text NOT NULL CHECK (probe ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
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
-- A live fence closes every claim of the host (placement, wakeable, attach),
-- except the exact identity the enrolled controller must run for its health
-- turn: the fence's own probe agent. `host_claims_open` stays the strict
-- predicate for scheduling decisions; this one is only used on the per-agent
-- claim and turn paths, where a normal agent still compares equal to strict.
CREATE FUNCTION ameesh_agent_claims_open(wanted_host text, wanted_agent text)
RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    IF ameesh_host_claims_open(wanted_host) THEN
        RETURN true;
    END IF;
    RETURN NOT EXISTS (SELECT 1 FROM host_lifecycle
                       WHERE host=wanted_host AND mode IN ('draining','lost'))
       AND EXISTS (SELECT 1 FROM host_release_fences
                   WHERE host=wanted_host AND probe=wanted_agent);
END $$;
-- Default executor grants must not confer release-controller writes.
-- Leave SELECT available to the invoker-rights claim predicate, revoke every
-- non-owner writer present at creation (including global/schema defaults) by
-- reading the ACL itself: a hard-coded privilege list was read by the
-- additive-only classifier as a table emptied, and named a PostgreSQL 17
-- privilege that older servers reject.
DO $release_acl$
DECLARE item record;
BEGIN
    FOR item IN SELECT DISTINCT c.relname,acl.grantee,acl.privilege_type
                  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                  CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) acl
                 WHERE n.nspname=current_schema()
                   AND c.relname IN ('host_release_operations','host_release_fences','host_release_fence_receipts')
                   AND acl.grantee<>c.relowner
                   AND acl.privilege_type<>'SELECT' LOOP
        EXECUTE format('REVOKE %s ON %I.%I FROM %s',
                       item.privilege_type,
                       current_schema(),item.relname,
                       CASE WHEN item.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(item.grantee)) END);
    END LOOP;
END
$release_acl$;
