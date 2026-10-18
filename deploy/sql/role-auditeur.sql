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
-- TRANSACTIONNEL ET IDEMPOTENT : une appartenance hors des deux contrats
-- ou des rôles de base absents font échouer le script et annulent tout. Le relancer après une mise à jour ne change
-- rien. Il remet à zéro les attributs (LOGIN INHERIT, aucun privilège de
-- rôle) et les droits directs du rôle sur le schéma ; il ne révoque jamais
-- une appartenance accordée par un autre : il refuse.
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
    extra text;
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

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
        EXECUTE format('CREATE ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION NOBYPASSRLS', r);
    ELSE
        EXECUTE format('ALTER ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION NOBYPASSRLS', r);
    END IF;

    -- Droits directs remis à zéro : seuls les contrats hérités comptent.
    IF s IS NOT NULL THEN
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', s, r);
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', s, r);
        EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', s, r);
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', s, r);
    END IF;

    EXECUTE format('GRANT %I TO %I', e, r);
    EXECUTE format('GRANT %I TO %I', c, r);
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
