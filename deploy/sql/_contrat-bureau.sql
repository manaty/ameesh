-- SPDX-License-Identifier: AGPL-3.0-only
-- #6881 audit seul OU inclus dans la transaction de role-bureau.sql.
-- Compare les droits EFFECTIFS (PUBLIC et appartenances compris) de la
-- connexion du bureau à la liste exacte de _bureau-droits.sql ; aucune
-- révocation ici. Administrateur, ON_ERROR_STOP, search_path du mesh.
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role bureau_goal
\endif
\if :{?agent}
\else
  \set agent bureau
\endif
\if :{?host}
\else
  \set host bureau-goal
\endif
SELECT set_config('ameesh.bureau_role', :'role', false),
       set_config('ameesh.bureau_agent', :'agent', false),
       set_config('ameesh.bureau_host', :'host', false) \g /dev/null
\ir _bureau-droits.sql
DO $audit$
DECLARE
    r text := current_setting('ameesh.bureau_role');
    a text := current_setting('ameesh.bureau_agent');
    h text := current_setting('ameesh.bureau_host');
    m text := 'ameesh_login_limite';
    s text := current_schema();
    n oid;
    rid oid;
    mid oid;
    item record;
    allowed boolean;
    actual boolean;
    errors text[] := '{}';
BEGIN
    n := (SELECT oid FROM pg_namespace WHERE nspname = s);
    rid := to_regrole(quote_ident(r));
    mid := to_regrole(quote_ident(m));
    IF n IS NULL OR rid IS NULL OR mid IS NULL THEN
        RAISE EXCEPTION 'bureau installation missing (schema %, role %, marker %)', s, r, m;
    END IF;
    -- inscription : la garde 0567 lit agent et hôte ici
    EXECUTE format('SELECT EXISTS (SELECT FROM %I.limited_logins WHERE rolname=$1 AND agent=$2 AND host=$3)', s)
       INTO actual USING r, a, h;
    IF NOT actual THEN errors := errors || format('limited_logins row %s/%s/%s', r, a, h); END IF;
    -- attributs : LOGIN pour la connexion, NOLOGIN pour le marqueur, aucun pouvoir
    IF EXISTS (SELECT FROM pg_roles WHERE oid IN (rid, mid) AND
        (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
         OR rolinherit OR rolcanlogin IS DISTINCT FROM (oid = rid))) THEN
        errors := errors || 'role attributes'::text;
    END IF;
    IF EXISTS (SELECT FROM pg_authid WHERE oid = rid AND rolpassword IS NOT NULL
               AND rolpassword NOT LIKE 'SCRAM-SHA-256$%') THEN
        errors := errors || 'password is not SCRAM'::text;
    END IF;
    IF EXISTS (SELECT FROM pg_authid WHERE oid = mid AND rolpassword IS NOT NULL) THEN
        errors := errors || 'marker has a password'::text;
    END IF;
    -- appartenances : la connexion est membre du seul marqueur ; le marqueur
    -- n'est membre de rien et n'a pour membres que des connexions limitées
    IF EXISTS (SELECT FROM pg_auth_members WHERE member = rid AND roleid <> mid)
       OR EXISTS (SELECT FROM pg_auth_members WHERE roleid = rid)
       OR EXISTS (SELECT FROM pg_auth_members WHERE member = mid)
       OR NOT EXISTS (SELECT FROM pg_auth_members WHERE member = rid AND roleid = mid
                      AND NOT admin_option AND NOT inherit_option AND NOT set_option) THEN
        errors := errors || 'role membership'::text;
    END IF;
    -- propriété : un propriétaire se redonne les droits retirés
    IF EXISTS (SELECT FROM pg_class WHERE relowner IN (rid, mid))
       OR EXISTS (SELECT FROM pg_namespace WHERE nspowner IN (rid, mid))
       OR EXISTS (SELECT FROM pg_database WHERE datdba IN (rid, mid))
       OR EXISTS (SELECT FROM pg_proc WHERE proowner IN (rid, mid))
       OR EXISTS (SELECT FROM pg_type WHERE typowner IN (rid, mid))
       OR EXISTS (SELECT FROM pg_largeobject_metadata WHERE lomowner IN (rid, mid))
       OR EXISTS (SELECT FROM pg_default_acl WHERE defaclrole IN (rid, mid)) THEN
        errors := errors || 'ownership'::text;
    END IF;
    -- aucun DDL ; aucun autre schéma applicatif
    IF has_schema_privilege(rid, n, 'CREATE') OR has_database_privilege(rid, current_database(), 'CREATE') THEN
        errors := errors || 'CREATE privilege'::text;
    END IF;
    IF NOT has_schema_privilege(rid, n, 'USAGE') THEN errors := errors || 'schema USAGE missing'::text; END IF;
    FOR item IN SELECT nspname FROM pg_namespace WHERE oid <> n
            AND nspname NOT IN ('pg_catalog','information_schema') AND nspname NOT LIKE 'pg\_%'
            AND (has_schema_privilege(rid, oid, 'CREATE')
                 OR (has_schema_privilege(rid, oid, 'USAGE') AND nspname <> 'public'))
    LOOP errors := errors || format('foreign schema %s', item.nspname); END LOOP;
    -- les relations nommées existent
    FOR item IN SELECT DISTINCT rel FROM pg_temp.bureau_spec
            WHERE to_regclass(format('%I.%I', s, rel)) IS NULL
    LOOP errors := errors || format('missing relation %s', item.rel); END LOOP;
    -- tables et vues : chaque privilège de table, puis chaque colonne
    FOR item IN SELECT c.oid, c.relname, p.priv FROM pg_class c,
            unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p(priv)
           WHERE c.relnamespace = n AND c.relkind IN ('r','v','m','p','f')
    LOOP
        BEGIN
            actual := has_table_privilege(rid, item.oid, item.priv);
        EXCEPTION WHEN undefined_table THEN CONTINUE;
        END;
        allowed := EXISTS (SELECT FROM pg_temp.bureau_spec x
                           WHERE x.rel = item.relname AND x.priv = item.priv AND x.cols IS NULL);
        IF actual IS DISTINCT FROM allowed AND actual IS NOT NULL THEN
            errors := errors || format('%s %s on %s', CASE WHEN actual THEN 'unexpected' ELSE 'missing' END,
                                       item.priv, item.relname);
        END IF;
    END LOOP;
    FOR item IN SELECT c.oid, c.relname, att.attnum, att.attname, p.priv FROM pg_class c
            JOIN pg_attribute att ON att.attrelid = c.oid AND att.attnum > 0 AND NOT att.attisdropped,
            unnest(ARRAY['SELECT','INSERT','UPDATE','REFERENCES']) p(priv)
           WHERE c.relnamespace = n AND c.relkind IN ('r','v','m','p','f')
    LOOP
        BEGIN
            actual := has_column_privilege(rid, item.oid, item.attnum, item.priv);
        EXCEPTION WHEN undefined_table OR undefined_column THEN CONTINUE;
        END;
        allowed := EXISTS (SELECT FROM pg_temp.bureau_spec x
                           WHERE x.rel = item.relname AND x.priv = item.priv
                             AND (x.cols IS NULL OR item.attname = ANY (x.cols || coalesce(x.opt_cols, '{}'))));
        IF actual IS DISTINCT FROM allowed AND actual IS NOT NULL THEN
            errors := errors || format('%s %s on %s.%s', CASE WHEN actual THEN 'unexpected' ELSE 'missing' END,
                                       item.priv, item.relname, item.attname);
        END IF;
    END LOOP;
    -- séquences : USAGE seulement, sur les seules séquences nommées
    FOR item IN SELECT c.oid, c.relname, p.priv FROM pg_class c,
            unnest(ARRAY['USAGE','SELECT','UPDATE']) p(priv)
           WHERE c.relnamespace = n AND c.relkind = 'S'
    LOOP
        BEGIN
            actual := has_sequence_privilege(rid, item.oid, item.priv);
        EXCEPTION WHEN undefined_table THEN CONTINUE;
        END;
        allowed := item.priv = 'USAGE' AND EXISTS (SELECT FROM pg_temp.bureau_spec x
                           WHERE x.rel = item.relname AND x.priv = 'USAGE');
        IF actual IS DISTINCT FROM allowed AND actual IS NOT NULL THEN
            errors := errors || format('%s %s on sequence %s', CASE WHEN actual THEN 'unexpected' ELSE 'missing' END,
                                       item.priv, item.relname);
        END IF;
    END LOOP;
    -- aucune fonction SECURITY DEFINER exécutable (elle agirait hors de la garde)
    FOR item IN SELECT p.oid::regprocedure AS fn FROM pg_proc p
           WHERE p.pronamespace = n AND p.prosecdef AND has_function_privilege(rid, p.oid, 'EXECUTE')
    LOOP errors := errors || format('executable SECURITY DEFINER %s', item.fn); END LOOP;
    -- la garde est en place sur chaque table écrite
    FOR item IN SELECT DISTINCT x.rel FROM pg_temp.bureau_spec x
           WHERE x.priv IN ('INSERT','UPDATE') AND NOT EXISTS (
               SELECT FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
                WHERE c.relnamespace = n AND c.relname = x.rel
                  AND t.tgname = 'zz_ameesh_limited_login' AND t.tgenabled IN ('O','A'))
    LOOP errors := errors || format('limited login guard missing on %s', item.rel); END LOOP;
    IF cardinality(errors) > 0 THEN
        RAISE EXCEPTION 'bureau contract violated: %', array_to_string(errors, '; ');
    END IF;
END
$audit$;
