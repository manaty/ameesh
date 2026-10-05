-- SPDX-License-Identifier: AGPL-3.0-only
-- Partie commune de role-superviseur.sql, role-superviseur-contenus.sql et
-- role-approve.sql (lot L27) — incluse par `\ir`, jamais lancée seule. Elle ne
-- connaît qu'un rôle en lecture seule et son contrat de colonnes, quel qu'il
-- soit. Elle s'exécute DANS la transaction
-- ouverte par le script appelant, qui a posé :
--   * le réglage local `ameesh.contrat_role` : le rôle visé ;
--   * la table temporaire `pg_temp.ameesh_contrat (rel, cols)` : EXACTEMENT
--     les colonnes que ce rôle doit pouvoir lire, et rien d'autre.
--
-- 1. Rôle : créé s'il manque, attributs remis à NOLOGIN et sans privilège.
-- 2. Ses PROPRES droits sur le schéma sont retirés, puis la lecture des
--    colonnes du contrat est accordée (droits de colonne : une colonne ajoutée
--    plus tard par une migration n'est jamais lisible sans décision).
-- 3. AUDIT des droits EFFECTIFS — directs, via PUBLIC, via les rôles dont il
--    hérite, via les rôles prédéfinis (pg_read_all_data…) — et des ACL par
--    défaut. Le script ne répare JAMAIS des droits accordés par d'autres (à
--    PUBLIC ou à un rôle parent) : si le contrat n'est pas tenu, il lève une
--    erreur qui liste chaque écart, et toute la transaction est annulée
--    (aucun rôle créé, aucun droit changé).
--    Le rôle ne doit être membre d'AUCUN rôle : une appartenance,
--    même sans héritage (INHERIT FALSE), permettrait SET ROLE vers un rôle
--    que l'audit des droits effectifs ne voit pas.
-- 4. AUDIT HORS DU SCHÉMA (lot L27) : aucun droit utile dans un AUTRE schéma
--    (lecture, écriture, séquence, fonction SECURITY DEFINER, CREATE ou
--    droit accordé sur le schéma), sur une base (CREATE, ou droit accordé au
--    rôle lui-même), ni sur un grand objet, ni par une ACL par défaut d'un
--    autre schéma (objets FUTURS : vers le rôle, tout type d'objet, même
--    sans USAGE ; vers PUBLIC, tables et séquences d'un schéma utilisable). Là aussi : refus et annulation,
--    jamais de révocation de ce que d'autres ont accordé.

DO $contrat$
DECLARE
    r text := current_setting('ameesh.contrat_role');
    s text := current_schema();
    s_oid oid;
    rel_name text;
    v_cols text[];
    manquantes text[];
    ecarts text[] := '{}';
    ligne record;
BEGIN
    IF s IS NULL THEN
        RAISE EXCEPTION 'aucun schéma dans le search_path (PGOPTIONS=''-c search_path=<schéma>'')';
    END IF;
    s_oid := (SELECT oid FROM pg_namespace WHERE nspname = s);

    -- base en retard (ou en avance) de migrations : on refuse avant tout
    SELECT array_agg(format('%s.%s', c.rel, c.col) ORDER BY 1) INTO manquantes
    FROM (SELECT t.rel, unnest(t.cols) AS col FROM pg_temp.ameesh_contrat t) c
    WHERE NOT EXISTS (
        SELECT 1 FROM pg_attribute a JOIN pg_class k ON k.oid = a.attrelid
        WHERE k.relnamespace = s_oid AND k.relname = c.rel AND a.attname = c.col
          AND a.attnum > 0 AND NOT a.attisdropped);
    IF manquantes IS NOT NULL THEN
        RAISE EXCEPTION 'contrat du rôle % : colonnes absentes du schéma % : % (migrations à jour ? script d''une autre version ?)',
                        r, s, array_to_string(manquantes, ', ');
    END IF;

    -- 1. le rôle
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
        EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                       'NOREPLICATION NOBYPASSRLS', r);
    ELSE
        EXECUTE format('ALTER ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                       'NOREPLICATION NOBYPASSRLS', r);
    END IF;

    -- 2. ses propres droits : remis à zéro (les droits de colonne tombent avec
    --    ceux de table), puis le contrat, colonne par colonne
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', s, r);
    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', s, r);
    EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', s, r);
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', s, r);
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', s, r);
    FOR rel_name, v_cols IN SELECT c.rel, c.cols FROM pg_temp.ameesh_contrat c LOOP
        EXECUTE format('GRANT SELECT (%s) ON %I.%I TO %I',
                       (SELECT string_agg(quote_ident(x), ', ') FROM unnest(v_cols) x),
                       s, rel_name, r);
    END LOOP;

    -- 3. audit des droits EFFECTIFS
    -- 3a. lecture : exactement les colonnes du contrat
    FOR ligne IN
        SELECT k.relname AS rel, a.attname AS col,
               has_column_privilege(r, k.oid, a.attnum, 'SELECT') AS lit,
               EXISTS (SELECT 1 FROM pg_temp.ameesh_contrat c
                       WHERE c.rel = k.relname AND a.attname = ANY (c.cols)) AS prevu
        FROM pg_class k
        JOIN pg_attribute a ON a.attrelid = k.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE k.relnamespace = s_oid AND k.relkind IN ('r', 'v', 'm', 'p', 'f')
        ORDER BY 1, a.attnum
    LOOP
        IF ligne.lit AND NOT ligne.prevu THEN
            ecarts := ecarts || format('lecture hors contrat : %s.%s', ligne.rel, ligne.col);
        ELSIF ligne.prevu AND NOT ligne.lit THEN
            ecarts := ecarts || format('lecture prévue absente : %s.%s', ligne.rel, ligne.col);
        END IF;
    END LOOP;
    -- 3b. aucune écriture, sur aucune relation (droits de table ou de colonne)
    FOR ligne IN
        SELECT k.relname AS rel, p.priv
        FROM pg_class k
        CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
                                'REFERENCES', 'TRIGGER']) AS p(priv)
        WHERE k.relnamespace = s_oid AND k.relkind IN ('r', 'v', 'm', 'p', 'f')
          AND (has_table_privilege(r, k.oid, p.priv)
               OR CASE WHEN p.priv IN ('INSERT', 'UPDATE', 'REFERENCES')
                       THEN has_any_column_privilege(r, k.oid, p.priv) END)
        ORDER BY 1, 2
    LOOP
        ecarts := ecarts || format('%s sur %s', ligne.priv, ligne.rel);
    END LOOP;
    -- 3c. aucune séquence
    FOR ligne IN
        SELECT k.relname AS rel FROM pg_class k
        WHERE k.relnamespace = s_oid
          AND CASE WHEN k.relkind = 'S'
                   THEN has_sequence_privilege(r, k.oid, 'USAGE, SELECT, UPDATE') END
    LOOP
        ecarts := ecarts || format('séquence %s', ligne.rel);
    END LOOP;
    -- 3d. aucune fonction SECURITY DEFINER exécutable (elle donnerait les
    --     droits de son propriétaire)
    FOR ligne IN
        SELECT p.oid::regprocedure::text AS f FROM pg_proc p
        WHERE p.pronamespace = s_oid AND p.prosecdef
          AND has_function_privilege(r, p.oid, 'EXECUTE')
    LOOP
        ecarts := ecarts || format('fonction SECURITY DEFINER exécutable : %s', ligne.f);
    END LOOP;
    -- 3e. schéma : aucune création
    IF has_schema_privilege(r, s, 'CREATE') THEN
        ecarts := ecarts || format('CREATE sur le schéma %s (PostgreSQL ≤ 14 : '
                                   'REVOKE CREATE ON SCHEMA public FROM PUBLIC)', s);
    END IF;
    -- 3f. AUCUNE appartenance : le rôle superviseur n'est membre d'AUCUN autre
    --     rôle (rôle parent, rôle prédéfini, ni l'autre rôle superviseur).
    --     Règle simple qui ferme toute la classe : sans appartenance, rien à
    --     hériter ET aucun rôle atteignable par SET ROLE (qui contournerait
    --     l'audit des droits effectifs : INHERIT FALSE, SET TRUE ; ou toute
    --     appartenance avant PostgreSQL 16). Les rôles de CONNEXION sont, eux,
    --     membres des rôles superviseurs, jamais l'inverse.
    FOR ligne IN
        SELECT m.roleid::regrole::text AS parent FROM pg_auth_members m
        WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = r)
        ORDER BY 1
    LOOP
        ecarts := ecarts || format('appartenance interdite : %s est membre de %s '
                                   '(héritage ou SET ROLE contourneraient le contrat ; '
                                   'REVOKE %s FROM %s)',
                                   r, ligne.parent, ligne.parent, r);
    END LOOP;
    -- 3g. attributs, y compris hérités d'un rôle parent (redondant avec 3f,
    --     gardé comme filet)
    IF EXISTS (SELECT 1 FROM pg_roles g
               WHERE pg_has_role(r, g.oid, 'MEMBER')
                 AND (g.rolsuper OR g.rolcreaterole OR g.rolcreatedb
                      OR g.rolreplication OR g.rolbypassrls)) THEN
        ecarts := ecarts || 'attribut privilégié (superuser, createrole, createdb, '
                            'replication ou bypassrls) sur le rôle ou un rôle dont il est membre';
    END IF;
    -- 3h. ACL par défaut : un objet FUTUR du schéma (table, vue, séquence)
    --     ne doit rien donner au rôle, ni à PUBLIC, ni à un rôle dont
    --     il hérite
    FOR ligne IN
        SELECT d.defaclobjtype AS t,
               CASE WHEN d.defaclnamespace = 0 THEN 'toute la base' ELSE s END AS portee,
               CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE x.grantee::regrole::text END AS qui,
               x.privilege_type AS priv,
               d.defaclrole::regrole::text AS createur
        FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) x
        WHERE d.defaclnamespace IN (0, s_oid)
          -- tables et séquences pour PUBLIC ; TOUT type d'objet (fonctions,
          -- types, schémas compris) pour le rôle ou un rôle dont il hérite
          AND ((x.grantee = 0 AND d.defaclobjtype IN ('r', 'S'))
               OR (x.grantee <> 0 AND pg_has_role(r, x.grantee, 'USAGE')))
          -- ACL par défaut implicites d'un créateur sur ses propres objets
          AND x.grantee <> d.defaclrole
        ORDER BY 1, 2, 3, 4
    LOOP
        ecarts := ecarts || format('ACL par défaut (%s, %s) : %s à %s sur les objets futurs créés par %s',
                                   ligne.t, ligne.portee, ligne.priv, ligne.qui, ligne.createur);
    END LOOP;

    -- 4. HORS DU SCHÉMA (verdict codex2, L27 B2) : le contrat ne vaut que
    --    pour CE schéma. Tout droit utile ailleurs — autre schéma (celui
    --    d'une autre équipe), autre base, grands objets — est un écart :
    --    refus et annulation, jamais une révocation silencieuse de droits
    --    accordés par d'autres. Schémas système exclus (pg_catalog,
    --    information_schema, pg_toast*, schémas temporaires).
    -- 4a. lecture, sur toute relation d'un autre schéma UTILISABLE par le
    --     rôle (USAGE effectif ; droits de table ou de colonne)
    FOR ligne IN
        SELECT n.nspname AS sch, k.relname AS rel
        FROM pg_class k JOIN pg_namespace n ON n.oid = k.relnamespace
        WHERE k.relnamespace <> s_oid
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname !~ '^pg_(toast|temp_|toast_temp_)'
          AND k.relkind IN ('r', 'v', 'm', 'p', 'f')
          AND has_schema_privilege(r, n.oid, 'USAGE')
          AND (has_table_privilege(r, k.oid, 'SELECT')
               OR has_any_column_privilege(r, k.oid, 'SELECT'))
        ORDER BY 1, 2
    LOOP
        ecarts := ecarts || format('lecture hors du schéma %s : %s.%s', s, ligne.sch, ligne.rel);
    END LOOP;
    -- 4b. écriture, sur toute relation d'un autre schéma
    FOR ligne IN
        SELECT n.nspname AS sch, k.relname AS rel, p.priv
        FROM pg_class k JOIN pg_namespace n ON n.oid = k.relnamespace
        CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
                                'REFERENCES', 'TRIGGER']) AS p(priv)
        WHERE k.relnamespace <> s_oid
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname !~ '^pg_(toast|temp_|toast_temp_)'
          AND k.relkind IN ('r', 'v', 'm', 'p', 'f')
          AND has_schema_privilege(r, n.oid, 'USAGE')
          AND (has_table_privilege(r, k.oid, p.priv)
               OR CASE WHEN p.priv IN ('INSERT', 'UPDATE', 'REFERENCES')
                       THEN has_any_column_privilege(r, k.oid, p.priv) END)
        ORDER BY 1, 2, 3
    LOOP
        ecarts := ecarts || format('%s hors du schéma %s : %s.%s', ligne.priv, s, ligne.sch,
                                   ligne.rel);
    END LOOP;
    -- 4c. séquences et fonctions SECURITY DEFINER des autres schémas
    FOR ligne IN
        SELECT n.nspname AS sch, k.relname AS rel
        FROM pg_class k JOIN pg_namespace n ON n.oid = k.relnamespace
        WHERE k.relnamespace <> s_oid
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname !~ '^pg_(toast|temp_|toast_temp_)'
          AND has_schema_privilege(r, n.oid, 'USAGE')
          AND CASE WHEN k.relkind = 'S'
                   THEN has_sequence_privilege(r, k.oid, 'USAGE, SELECT, UPDATE') END
        ORDER BY 1, 2
    LOOP
        ecarts := ecarts || format('séquence hors du schéma %s : %s.%s', s, ligne.sch, ligne.rel);
    END LOOP;
    FOR ligne IN
        SELECT p.oid::regprocedure::text AS f
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE p.pronamespace <> s_oid AND p.prosecdef
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND has_schema_privilege(r, n.oid, 'USAGE')
          AND has_function_privilege(r, p.oid, 'EXECUTE')
        ORDER BY 1
    LOOP
        ecarts := ecarts || format('fonction SECURITY DEFINER exécutable hors du schéma %s : %s',
                                   s, ligne.f);
    END LOOP;
    -- 4c'. droits accordés AU RÔLE sur un objet d'un autre schéma, même
    --      inutilisables faute d'USAGE aujourd'hui (latents)
    FOR ligne IN
        SELECT DISTINCT n.nspname AS sch, k.relname AS rel
        FROM pg_class k JOIN pg_namespace n ON n.oid = k.relnamespace
        WHERE k.relnamespace <> s_oid
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND (EXISTS (SELECT 1 FROM aclexplode(k.relacl) x
                       WHERE x.grantee = (SELECT oid FROM pg_roles WHERE rolname = r))
               OR EXISTS (SELECT 1 FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) x
                          WHERE a.attrelid = k.oid
                            AND x.grantee = (SELECT oid FROM pg_roles WHERE rolname = r)))
        ORDER BY 1, 2
    LOOP
        ecarts := ecarts || format('droit accordé au rôle hors du schéma %s : %s.%s', s,
                                   ligne.sch, ligne.rel);
    END LOOP;
    -- 4d. autres schémas : aucune création ; aucun USAGE accordé AU RÔLE
    --     (l'USAGE de PUBLIC sur `public`, par défaut, ne donne rien sans
    --     droit sur un objet, que 4a–4c refusent)
    FOR ligne IN
        SELECT n.nspname AS sch,
               has_schema_privilege(r, n.oid, 'CREATE') AS cree,
               EXISTS (SELECT 1 FROM aclexplode(n.nspacl) x
                       WHERE x.grantee = (SELECT oid FROM pg_roles WHERE rolname = r)) AS direct
        FROM pg_namespace n
        WHERE n.oid <> s_oid
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname !~ '^pg_(toast|temp_|toast_temp_)'
        ORDER BY 1
    LOOP
        IF ligne.cree THEN
            ecarts := ecarts || format('CREATE sur le schéma %s (hors du schéma %s)', ligne.sch, s);
        END IF;
        IF ligne.direct THEN
            ecarts := ecarts || format('droit accordé au rôle sur le schéma %s (hors du schéma %s)',
                                       ligne.sch, s);
        END IF;
    END LOOP;
    -- 4e. bases : aucune création (CREATE) nulle part ; aucun droit
    --     (CONNECT, TEMP, CREATE) accordé AU RÔLE lui-même — la connexion
    --     est l'affaire du rôle de CONNEXION, pas du rôle de contrat
    FOR ligne IN
        SELECT d.datname AS base,
               has_database_privilege(r, d.oid, 'CREATE') AS cree,
               (SELECT string_agg(DISTINCT x.privilege_type, ', ')
                  FROM aclexplode(d.datacl) x
                 WHERE x.grantee = (SELECT oid FROM pg_roles WHERE rolname = r)) AS direct
        FROM pg_database d
        WHERE d.datallowconn
        ORDER BY 1
    LOOP
        IF ligne.cree THEN
            ecarts := ecarts || format('CREATE sur la base %s', ligne.base);
        END IF;
        IF ligne.direct IS NOT NULL THEN
            ecarts := ecarts || format('droit accordé au rôle sur la base %s : %s', ligne.base,
                                       ligne.direct);
        END IF;
    END LOOP;
    -- 4e'. ACL par défaut des AUTRES schémas (verdict codex2, L27) : un objet
    --      FUTUR d'un autre schéma ne doit rien donner au rôle — même sans
    --      USAGE aujourd'hui (droit latent), quel que soit le type d'objet —
    --      ni à PUBLIC (tables, séquences) si le schéma est utilisable par
    --      le rôle
    FOR ligne IN
        SELECT d.defaclobjtype AS t, n.nspname AS sch,
               CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE x.grantee::regrole::text END AS qui,
               x.privilege_type AS priv,
               d.defaclrole::regrole::text AS createur
        FROM pg_default_acl d
        JOIN pg_namespace n ON n.oid = d.defaclnamespace
        CROSS JOIN LATERAL aclexplode(d.defaclacl) x
        WHERE d.defaclnamespace NOT IN (0, s_oid)
          AND x.grantee <> d.defaclrole
          AND ((x.grantee <> 0 AND pg_has_role(r, x.grantee, 'USAGE'))
               OR (x.grantee = 0 AND d.defaclobjtype IN ('r', 'S')
                   AND has_schema_privilege(r, n.oid, 'USAGE')))
        ORDER BY 2, 1, 3, 4
    LOOP
        ecarts := ecarts || format('ACL par défaut hors du schéma %s (%s, %s) : %s à %s sur les '
                                   'objets futurs créés par %s', s, ligne.t, ligne.sch,
                                   ligne.priv, ligne.qui, ligne.createur);
    END LOOP;
    -- 4f. grands objets : aucun lisible ni modifiable (accordé au rôle ou à
    --     PUBLIC), et pas de lo_compat_privileges (qui les ouvre tous)
    IF current_setting('lo_compat_privileges')::boolean THEN
        ecarts := ecarts || 'lo_compat_privileges = on : tous les grands objets sont lisibles';
    END IF;
    FOR ligne IN
        SELECT m.oid AS lo, string_agg(DISTINCT x.privilege_type, ', ') AS privs
        FROM pg_largeobject_metadata m CROSS JOIN LATERAL aclexplode(m.lomacl) x
        WHERE x.grantee IN (0, (SELECT oid FROM pg_roles WHERE rolname = r))
        GROUP BY m.oid ORDER BY 1
    LOOP
        ecarts := ecarts || format('grand objet %s : %s', ligne.lo, ligne.privs);
    END LOOP;

    IF array_length(ecarts, 1) IS NOT NULL THEN
        -- un seul littéral : RAISE n'accepte pas la concaténation implicite
        RAISE EXCEPTION E'contrat du rôle % NON TENU dans le schéma % (% écart(s)) — installation annulée, rien n''a changé :\n  %\nCes droits viennent d''ailleurs (PUBLIC, rôle parent, ACL par défaut) : les retirer à la source, puis relancer.',
                        r, s, array_length(ecarts, 1), array_to_string(ecarts, E'\n  ');
    END IF;
    RAISE NOTICE 'contrat du rôle % tenu dans le schéma %', r, s;
END
$contrat$;
