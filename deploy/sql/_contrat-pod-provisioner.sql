-- SPDX-License-Identifier: AGPL-3.0-only
-- #7697 / verdict #44561 : audit seul du contrat des rôles DB du pourvoyeur
-- et du webhook d'admission. À rejouer après chaque migration additive :
--   PGOPTIONS='-c search_path=<schéma mesh>' psql <DSN admin> -X \
--     -v ON_ERROR_STOP=1 -f deploy/sql/_contrat-pod-provisioner.sql
-- Aucune révocation ici : l'audit refuse, il ne répare pas.
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role ameesh_pod_provisioner
\endif
\if :{?admission}
\else
  \set admission ameesh_pod_admission
\endif
SELECT set_config('ameesh.pod_role', :'role', false),
       set_config('ameesh.pod_admission', :'admission', false) \g /dev/null
DO $audit$
DECLARE
    r text := current_setting('ameesh.pod_role');
    a text := current_setting('ameesh.pod_admission');
    s text := current_schema();
    rid oid;
    aid oid;
    n oid;
    item record;
    who text;
    priv text;
    errors text[] := '{}';
BEGIN
    n := (SELECT oid FROM pg_namespace WHERE nspname = s);
    SELECT oid INTO rid FROM pg_roles WHERE rolname = r;
    SELECT oid INTO aid FROM pg_roles WHERE rolname = a;
    IF n IS NULL OR rid IS NULL OR aid IS NULL OR r = a THEN
        RAISE EXCEPTION 'pod roles installation missing';
    END IF;
    FOREACH who IN ARRAY ARRAY[r, a] LOOP
        IF EXISTS (SELECT FROM pg_roles x
                    WHERE x.rolname = who
                      AND (x.rolsuper OR x.rolcreatedb OR x.rolcreaterole
                           OR x.rolreplication OR x.rolbypassrls
                           OR x.rolinherit
                           OR NOT x.rolcanlogin)) THEN
            errors := errors || format('role attributes %s', who);
        END IF;
        IF EXISTS (SELECT FROM pg_auth_members m
                    JOIN pg_roles x ON x.oid = m.member
                   WHERE x.rolname = who) THEN
            errors := errors || format('role membership %s', who);
        END IF;
        IF NOT has_schema_privilege(who, n, 'USAGE') THEN
            errors := errors || format('missing schema usage %s', who);
        END IF;
        IF has_schema_privilege(who, n, 'CREATE') THEN
            errors := errors || format('schema create %s', who);
        END IF;
    END LOOP;

    -- pod_requests : SELECT pour les deux ; UPDATE d'exploitation pour le
    -- seul pourvoyeur, jamais une colonne d'autorisation.
    IF NOT has_table_privilege(r, format('%I.pod_requests', s), 'SELECT')
       OR NOT has_table_privilege(a, format('%I.pod_requests', s), 'SELECT') THEN
        errors := errors || 'pod_requests SELECT missing';
    END IF;
    FOREACH priv IN ARRAY ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
                                'REFERENCES', 'TRIGGER'] LOOP
        FOREACH who IN ARRAY ARRAY[r, a] LOOP
            IF has_table_privilege(who, format('%I.pod_requests', s), priv) THEN
                errors := errors || format('pod_requests %s %s', priv, who);
            END IF;
        END LOOP;
    END LOOP;
    FOR item IN
        SELECT a2.attname FROM pg_class c
          JOIN pg_namespace ns ON ns.oid = c.relnamespace
          JOIN pg_attribute a2 ON a2.attrelid = c.oid
         WHERE ns.nspname = s AND c.relname = 'pod_requests'
           AND a2.attnum > 0 AND NOT a2.attisdropped
    LOOP
        IF has_column_privilege(r, format('%I.pod_requests', s), item.attname,
                                'INSERT')
           OR has_column_privilege(a, format('%I.pod_requests', s), item.attname,
                                   'INSERT') THEN
            errors := errors || format('pod_requests INSERT %s', item.attname);
        END IF;
        IF has_column_privilege(a, format('%I.pod_requests', s), item.attname,
                                'UPDATE') THEN
            errors := errors || format('pod_requests admission UPDATE %s',
                                       item.attname);
        END IF;
        IF has_column_privilege(r, format('%I.pod_requests', s), item.attname,
                                'UPDATE')
           IS DISTINCT FROM (item.attname = ANY (ARRAY[
               'state', 'pod_name', 'claimed_by', 'claimed_at', 'started_at',
               'finished_at', 'error'])) THEN
            errors := errors || format('pod_requests UPDATE outside contract %s',
                                       item.attname);
        END IF;
    END LOOP;

    -- Les quatre tables d'exploitation du pourvoyeur.
    IF NOT has_table_privilege(r, format('%I.pod_templates', s), 'SELECT')
       OR NOT has_table_privilege(r, format('%I.pod_templates', s), 'INSERT')
       OR NOT has_table_privilege(r, format('%I.pod_templates', s), 'UPDATE')
       OR has_table_privilege(r, format('%I.pod_templates', s), 'DELETE') THEN
        errors := errors || 'pod_templates privileges outside contract';
    END IF;
    IF NOT has_table_privilege(r, format('%I.pod_hosts', s), 'SELECT')
       OR NOT has_table_privilege(r, format('%I.pod_hosts', s), 'INSERT')
       OR NOT has_table_privilege(r, format('%I.pod_hosts', s), 'UPDATE')
       OR has_table_privilege(r, format('%I.pod_hosts', s), 'DELETE') THEN
        errors := errors || 'pod_hosts privileges outside contract';
    END IF;
    FOREACH priv IN ARRAY ARRAY['host_tenancy', 'host_tenants'] LOOP
        IF NOT has_table_privilege(r, format('%I.%I', s, priv), 'SELECT')
           OR NOT has_table_privilege(r, format('%I.%I', s, priv), 'INSERT')
           OR NOT has_table_privilege(r, format('%I.%I', s, priv), 'UPDATE')
           OR NOT has_table_privilege(r, format('%I.%I', s, priv), 'DELETE') THEN
            errors := errors || format('%s privileges outside contract', priv);
        END IF;
    END LOOP;
    -- agent_registry : deux colonnes lues, rien d'autre.
    FOR item IN
        SELECT a2.attname FROM pg_class c
          JOIN pg_namespace ns ON ns.oid = c.relnamespace
          JOIN pg_attribute a2 ON a2.attrelid = c.oid
         WHERE ns.nspname = s AND c.relname = 'agent_registry'
           AND a2.attnum > 0 AND NOT a2.attisdropped
    LOOP
        IF has_column_privilege(r, format('%I.agent_registry', s), item.attname,
                                'SELECT')
           IS DISTINCT FROM (item.attname = ANY (ARRAY['name', 'host'])) THEN
            errors := errors || format('agent_registry SELECT outside contract %s',
                                       item.attname);
        END IF;
    END LOOP;

    -- Le webhook n'a aucun droit hors de pod_requests, et le pourvoyeur aucun
    -- droit hors des tables ci-dessus.
    FOR item IN
        SELECT c.oid, c.relname FROM pg_class c
          JOIN pg_namespace ns ON ns.oid = c.relnamespace
         WHERE ns.nspname = s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND c.relname NOT IN ('pod_requests', 'pod_templates', 'pod_hosts',
                                 'host_tenancy', 'host_tenants', 'agent_registry')
    LOOP
        FOREACH who IN ARRAY ARRAY[r, a] LOOP
            FOREACH priv IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE',
                                        'TRUNCATE', 'REFERENCES', 'TRIGGER'] LOOP
                IF has_table_privilege(who, item.oid, priv) THEN
                    errors := errors || format('%s %s %s', priv, item.relname, who);
                END IF;
            END LOOP;
            IF EXISTS (
                SELECT FROM pg_attribute a2
                  CROSS JOIN LATERAL aclexplode(a2.attacl) x
                 WHERE a2.attrelid = item.oid
                   AND x.grantee = CASE who WHEN r THEN rid ELSE aid END) THEN
                errors := errors || format('column grant %s %s', item.relname, who);
            END IF;
        END LOOP;
    END LOOP;
    -- Et aucun droit sur les séquences du schéma.
    FOR item IN SELECT c.oid, c.relname FROM pg_class c
          JOIN pg_namespace ns ON ns.oid = c.relnamespace
         WHERE ns.nspname = s AND c.relkind = 'S'
    LOOP
        FOREACH who IN ARRAY ARRAY[r, a] LOOP
            FOREACH priv IN ARRAY ARRAY['USAGE', 'SELECT', 'UPDATE'] LOOP
                IF has_sequence_privilege(who, item.oid, priv) THEN
                    errors := errors || format('sequence %s %s %s', item.relname,
                                               priv, who);
                END IF;
            END LOOP;
        END LOOP;
    END LOOP;

    -- #44568/#44569 : RLS active sur les trois tables de liaison, policy
    -- permissive pour tous, policy restrictive pour le seul pourvoyeur,
    -- bornée par les hôtes de `pod_requests`.
    FOR item IN
        SELECT unnest(ARRAY['pod_hosts', 'host_tenancy', 'host_tenants']) AS rel
    LOOP
        IF NOT EXISTS (SELECT FROM pg_class c
                        WHERE c.relnamespace = n AND c.relname = item.rel
                          AND c.relkind IN ('r', 'p') AND c.relrowsecurity) THEN
            errors := errors || format('RLS missing %s', item.rel);
        END IF;
        IF NOT EXISTS (SELECT FROM pg_policies p
                        WHERE p.schemaname = s AND p.tablename = item.rel
                          AND p.policyname = 'pod_scope_provisioner'
                          AND p.permissive = 'RESTRICTIVE'
                          AND r = ANY (p.roles)
                          AND p.qual LIKE '%pod_requests%'
                          AND p.with_check LIKE '%pod_requests%') THEN
            errors := errors || format('restrictive policy missing %s', item.rel);
        END IF;
        IF NOT EXISTS (SELECT FROM pg_policies p
                        WHERE p.schemaname = s AND p.tablename = item.rel
                          AND p.policyname = 'pod_scope_open'
                          AND p.permissive = 'PERMISSIVE'
                          AND 'public' = ANY (p.roles)) THEN
            errors := errors || format('permissive policy missing %s', item.rel);
        END IF;
    END LOOP;

    IF array_length(errors, 1) > 0 THEN
        RAISE EXCEPTION 'contract refused: %', array_to_string(errors, '; ');
    END IF;
END
$audit$;
