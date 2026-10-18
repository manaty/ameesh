-- SPDX-License-Identifier: AGPL-3.0-only
-- Rôle de CONNEXION en lecture seule de l'auditeur interne (#2340).
--
--   PGOPTIONS='-c search_path=<schéma ameesh>' \
--   psql "<DSN d'un rôle qui peut CREATE ROLE>" -v ON_ERROR_STOP=1 \
--        [-v role=auditeur_lecture] [-v etat=ameesh_superviseur] \
--        [-v contenus=ameesh_superviseur_contenus] \
--        -f deploy/sql/role-auditeur.sql
--
-- À lancer par le PROPRIÉTAIRE (c'est une permission), APRÈS les deux rôles
-- du contrat de supervision :
--   deploy/sql/role-superviseur.sql           -> ameesh_superviseur (état)
--   deploy/sql/role-superviseur-contenus.sql  -> ameesh_superviseur_contenus
--
-- Le rôle de connexion est membre des deux contrats et n'a aucun droit
-- direct : ses droits propres sur le schéma sont retirés, seuls les contrats
-- hérités comptent. `default_transaction_read_only=on` borne la session, mais
-- la garantie reste les GRANT de colonne des contrats. Le mot de passe n'est
-- JAMAIS dans ce fichier : le propriétaire pose `\password <role>`, puis une
-- ligne dans le `~/.pgpass` (0600) de l'hôte de l'auditeur.
--
-- TRANSACTIONNEL ET IDEMPOTENT : une appartenance hors des deux contrats ou
-- des rôles de base absents font échouer le script et annulent tout. Le
-- relancer après une mise à jour ne change rien. Il remet à zéro les
-- attributs (LOGIN INHERIT, aucun privilège de rôle) et les droits directs du
-- rôle sur le schéma ; il ne révoque jamais une appartenance accordée par un
-- autre : il refuse.
--
-- Propriété refusée : le rôle ne doit RIEN posséder (schéma, table, séquence,
-- fonction, type, base, privilèges par défaut), ni les contrats dont il hérite
-- (atteignables par SET ROLE) : un propriétaire garde le pouvoir de se
-- redonner les droits que le REVOKE lui retire. Refuse aussi un réglage
-- `ALTER ROLE … IN DATABASE … SET default_transaction_read_only`, qui
-- supplante le réglage du rôle.
--
-- PG 16+ : une appartenance préexistante accordée alors que le rôle était
-- NOINHERIT garde `inherit_option=false` ; le script force `INHERIT TRUE` sur
-- les deux contrats et relit l'héritage, puis les droits effectifs (une
-- colonne d'état et une de contenus).
--
-- Sans ce rôle (ou sans `AUDITEUR_DSN` dans le drop-in de l'exécuteur),
-- l'analyse se contente des commandes `ameesh … --json` ; elle n'utilise
-- jamais `AGENT_MESH_DSN`, qui n'est pas borné à la lecture.

\if :{?role}
\else
  \set role auditeur_lecture
\endif
\if :{?etat}
\else
  \set etat ameesh_superviseur
\endif
\if :{?contenus}
\else
  \set contenus ameesh_superviseur_contenus
\endif

BEGIN;

SELECT set_config('ameesh.auditeur_role', :'role', true) \g /dev/null
SELECT set_config('ameesh.auditeur_etat', :'etat', true) \g /dev/null
SELECT set_config('ameesh.auditeur_contenus', :'contenus', true) \g /dev/null

DO $auditeur$
DECLARE
    r text := current_setting('ameesh.auditeur_role');
    e text := current_setting('ameesh.auditeur_etat');
    c text := current_setting('ameesh.auditeur_contenus');
    s text := current_schema();
    r_oid oid;
    extra text;
    possede text;
    reglages text;
    admin boolean;
BEGIN
    IF r = e OR r = c OR e = c THEN
        RAISE EXCEPTION 'rôles confondus (role=%, etat=%, contenus=%) ; transaction annulée',
                        r, e, c;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = e)
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = c) THEN
        RAISE EXCEPTION 'rôles de base absents (% , %) : lancer deploy/sql/role-superviseur.sql et deploy/sql/role-superviseur-contenus.sql d''abord',
                        e, c;
    END IF;
    IF s IS NULL THEN
        RAISE EXCEPTION 'search_path ne désigne aucun schéma existant : poser PGOPTIONS=''-c search_path=<schéma ameesh>'' ; transaction annulée';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
        EXECUTE format('CREATE ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION NOBYPASSRLS', r);
    ELSE
        EXECUTE format('ALTER ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION NOBYPASSRLS', r);
    END IF;

    SELECT oid INTO r_oid FROM pg_roles WHERE rolname = r;

    -- Propriété refusée : un propriétaire garde le pouvoir de se redonner les
    -- droits que le REVOKE lui retire (GRANT à soi-même, ALTER OWNER, etc.).
    -- pg_shdepend couvre les schémas, tables, séquences, fonctions, types,
    -- bases et privilèges par défaut, dans toutes les bases ; les contrats
    -- hérités sont atteignables par SET ROLE et sont donc contrôlés aussi.
    SELECT string_agg(format('%s possède %s', u.rolname,
                             coalesce(nullif(pg_describe_object(d.classid, d.objid, 0), ''),
                                      d.classid::regclass::text || ' oid ' || d.objid)),
                      ' ; ' ORDER BY 1)
      INTO possede
      FROM pg_shdepend d
      JOIN pg_roles u ON u.oid = d.refobjid
     WHERE u.rolname IN (r, e, c)
       AND d.refclassid = 'pg_authid'::regclass
       AND d.deptype = 'o';
    IF possede IS NOT NULL THEN
        RAISE EXCEPTION 'propriété interdite : % — transférez la propriété (ALTER ... OWNER TO ...), le REVOKE ne désarme pas un propriétaire ; transaction annulée',
                        possede;
    END IF;

    -- Un réglage `ALTER ROLE … IN DATABASE … SET default_transaction_read_only`
    -- supplante le réglage du rôle : la lecture seule ne serait plus le défaut.
    SELECT string_agg(format('%s : %s', d.datname, reglage), ' ; ' ORDER BY 1)
      INTO reglages
      FROM pg_db_role_setting rs
      JOIN pg_database d ON d.oid = rs.setdatabase
      CROSS JOIN LATERAL unnest(rs.setconfig) AS reglage
     WHERE rs.setrole = r_oid
       AND rs.setdatabase <> 0
       AND reglage LIKE 'default_transaction_read_only=%';
    IF reglages IS NOT NULL THEN
        RAISE EXCEPTION 'réglage par base interdit pour % : % — ALTER ROLE … IN DATABASE … RESET default_transaction_read_only ; transaction annulée',
                        r, reglages;
    END IF;

    -- Droits directs remis à zéro : seuls les contrats hérités comptent.
    IF s IS NOT NULL THEN
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', s, r);
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', s, r);
        EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', s, r);
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', s, r);
    END IF;

    -- PG 16+ : une appartenance accordée alors que le rôle était NOINHERIT
    -- garde inherit_option=false, et un GRANT sans option ne la corrige pas
    -- (notice « already granted ») : la lecture par les contrats exigerait
    -- SET ROLE. On force l'héritage ; SET reste tel quel, il ne conditionne
    -- pas la lecture.
    IF current_setting('server_version_num')::int >= 160000 THEN
        EXECUTE format('GRANT %I TO %I WITH INHERIT TRUE', e, r);
        EXECUTE format('GRANT %I TO %I WITH INHERIT TRUE', c, r);
    ELSE
        EXECUTE format('GRANT %I TO %I', e, r);
        EXECUTE format('GRANT %I TO %I', c, r);
    END IF;
    EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = on', r);

    -- Read-back : l'appartenance, les attributs et la lecture seule sont
    -- relus dans les catalogues, pas supposés par les ordres ci-dessus.
    SELECT string_agg(g.rolname, ', ' ORDER BY g.rolname) INTO extra
      FROM pg_auth_members m
      JOIN pg_roles g ON g.oid = m.roleid
      JOIN pg_roles u ON u.oid = m.member
     WHERE u.rolname = r AND g.rolname NOT IN (e, c);
    IF extra IS NOT NULL THEN
        RAISE EXCEPTION 'appartenance hors contrat pour % : % (seuls % et % sont admis) ; transaction annulée',
                        r, extra, e, c;
    END IF;

    SELECT bool_or(m.admin_option) INTO admin
      FROM pg_auth_members m
      JOIN pg_roles g ON g.oid = m.roleid
      JOIN pg_roles u ON u.oid = m.member
     WHERE u.rolname = r AND g.rolname IN (e, c);
    IF coalesce(admin, false) THEN
        RAISE EXCEPTION 'appartenance avec ADMIN OPTION pour % ; transaction annulée', r;
    END IF;

    -- PG 16+ : relire l'héritage des deux appartenances, pas seulement le
    -- GRANT. Une appartenance INHERIT FALSE passerait les contrôles ci-dessus
    -- sans donner la lecture (B2, #2340).
    IF current_setting('server_version_num')::int >= 160000 THEN
        IF EXISTS (SELECT 1
                     FROM pg_auth_members m
                     JOIN pg_roles g ON g.oid = m.roleid
                     JOIN pg_roles u ON u.oid = m.member
                    WHERE u.rolname = r
                      AND g.rolname IN (e, c)
                      AND NOT m.inherit_option) THEN
            RAISE EXCEPTION 'appartenance non héritée pour % (INHERIT FALSE) : la lecture par les contrats échouerait ; transaction annulée', r;
        END IF;
    END IF;

    -- Droits EFFECTIFS : lire une colonne de chaque contrat prouve l'héritage
    -- au-delà des catalogues d'appartenance (une colonne d'état, une de
    -- contenus ; les droits directs ont été retirés plus haut).
    IF NOT has_column_privilege(r, format('%I.%I', s, 'agent_mailbox'), 'id', 'SELECT')
       OR NOT has_column_privilege(r, format('%I.%I', s, 'agent_mailbox'), 'body', 'SELECT') THEN
        RAISE EXCEPTION 'droits effectifs absents pour % : lecture de l''état ou des contenus non héritée ; transaction annulée', r;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles
                WHERE rolname = r
                  AND (rolsuper OR rolcreatedb OR rolcreaterole
                       OR rolreplication OR rolbypassrls)) THEN
        RAISE EXCEPTION 'privilège interdit sur % ; transaction annulée', r;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles
                    WHERE rolname = r AND rolcanlogin AND rolinherit
                      AND coalesce(rolconfig, '{}')
                          @> ARRAY['default_transaction_read_only=on']) THEN
        RAISE EXCEPTION 'rôle % non LOGIN, non INHERIT ou lecture seule non posée ; transaction annulée', r;
    END IF;
END
$auditeur$;

COMMIT;
