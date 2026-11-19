-- SPDX-License-Identifier: AGPL-3.0-only
-- #7873 : pose des droits de la connexion limitée du bureau, inclus par
-- role-bureau.sql (installation, administrateur) et rejeu-droits-limites.sql
-- (staging, après les migrations, connexion de `migrate` propriétaire des
-- tables). GRANT et REVOKE seulement : ni rôle, ni mot de passe, ni
-- inscription. Lit `ameesh.bureau_role` et pg_temp.bureau_spec
-- (_bureau-droits.sql) ; refuse un rôle qui n'est pas une connexion limitée
-- inscrite (le rejeu n'ouvre jamais ces droits à un autre rôle).
DO $poser$
DECLARE
    r text := current_setting('ameesh.bureau_role');
    m text := 'ameesh_login_limite';
    s text := current_schema();
    item record;
    cols text[];
    enrolled boolean;
BEGIN
    IF to_regrole(quote_ident(r)) IS NULL OR to_regrole(quote_ident(m)) IS NULL
       OR NOT pg_has_role(to_regrole(quote_ident(r)), to_regrole(quote_ident(m)), 'MEMBER')
       OR to_regclass(format('%I.limited_logins', s)) IS NULL THEN
        RAISE EXCEPTION 'bureau grants refused: % is not a limited login (schema %)', r, s;
    END IF;
    EXECUTE format('SELECT EXISTS (SELECT FROM %I.limited_logins WHERE rolname = $1)', s)
       INTO enrolled USING r;
    IF NOT enrolled THEN
        RAISE EXCEPTION 'bureau grants refused: % is not enrolled in %.limited_logins', r, s;
    END IF;
    -- droits DIRECTS remis à zéro, puis la liste exacte
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', s, r);
    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', s, r);
    FOR item IN SELECT c.relname, a2.attname FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a2 ON a2.attrelid = c.oid
           WHERE n.nspname = s AND c.relkind IN ('r','v','m','p','f')
             AND a2.attnum > 0 AND NOT a2.attisdropped
             AND EXISTS (SELECT FROM aclexplode(a2.attacl) x
                          WHERE x.grantee = to_regrole(quote_ident(r)))
    LOOP
        EXECUTE format('REVOKE ALL (%I) ON %I.%I FROM %I', item.attname, s, item.relname, r);
    END LOOP;
    FOR item IN SELECT * FROM pg_temp.bureau_spec LOOP
        IF to_regclass(format('%I.%I', s, item.rel)) IS NULL THEN
            RAISE EXCEPTION 'bureau grant on missing relation %.%', s, item.rel;
        END IF;
        IF item.priv = 'USAGE' THEN
            EXECUTE format('GRANT USAGE ON SEQUENCE %I.%I TO %I', s, item.rel, r);
        ELSIF item.cols IS NULL THEN
            EXECUTE format('GRANT %s ON %I.%I TO %I', item.priv, s, item.rel, r);
        ELSE
            cols := item.cols || ARRAY(
                SELECT c FROM unnest(item.opt_cols) c
                 WHERE EXISTS (SELECT FROM pg_attribute a3
                                WHERE a3.attrelid = to_regclass(format('%I.%I', s, item.rel))
                                  AND a3.attname = c AND a3.attnum > 0 AND NOT a3.attisdropped));
            IF cardinality(cols) > 0 THEN
                EXECUTE format('GRANT %s (%s) ON %I.%I TO %I', item.priv,
                    (SELECT string_agg(quote_ident(c), ',') FROM unnest(cols) c),
                    s, item.rel, r);
            END IF;
        END IF;
    END LOOP;
END
$poser$;
