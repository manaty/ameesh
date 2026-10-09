-- SPDX-License-Identifier: AGPL-3.0-only
-- Rôle Postgres des EXÉCUTEURS (lot L56, étude v2 D4) : lire et écrire les
-- données du mesh, jamais modifier son schéma.
--
-- Le rôle qui POSSÈDE le schéma (celui qui lance `ameesh migrate`) n'est plus
-- celui des exécuteurs : un harnais lancé par un exécuteur hérite de son accès
-- à la base, et ne doit pas pouvoir créer, modifier ou supprimer une table.
--
--   PGOPTIONS='-c search_path=<schéma ameesh>' \
--   psql "<DSN du rôle PROPRIÉTAIRE du schéma>" -v ON_ERROR_STOP=1 \
--        [-v role=ameesh_executeur] -f deploy/sql/role-executeur.sql
--
-- À lancer SOUS LE RÔLE PROPRIÉTAIRE : les droits par défaut posés ici
-- couvrent les tables que ce rôle créera aux migrations suivantes. Relancer
-- après chaque mise à jour reste sans risque (idempotent).
--
-- Le rôle est NOLOGIN. Chaque hôte se connecte sous son propre rôle de
-- connexion, membre de celui-ci (pour révoquer un hôte sans toucher aux autres) :
--
--   CREATE ROLE ameesh_hote_pc_smichea LOGIN IN ROLE ameesh_executeur;
--   -- mot de passe : \password ameesh_hote_pc_smichea (jamais dans un fichier versionné)
--
-- TRANSACTIONNEL ET AUDITÉ : le script ouvre sa transaction, crée le rôle s'il
-- manque, accorde les droits, puis vérifie que le rôle ne peut PAS changer le
-- schéma : pas de CREATE sur le schéma, aucun objet possédé, pas membre du
-- propriétaire, aucun attribut d'administration (superuser, createdb,
-- createrole, bypassrls). Un écart fait échouer le script et ANNULE tout.
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role ameesh_executeur
\endif

BEGIN;
SELECT current_schema() AS schema, current_user AS proprietaire \gset
SELECT set_config('ameesh.role', :'role', true), set_config('ameesh.schema', :'schema', true);

SELECT format('CREATE ROLE %I NOLOGIN', :'role')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec

SELECT format('GRANT USAGE ON SCHEMA %I TO %I', :'schema', :'role') \gexec
SELECT format('REVOKE CREATE ON SCHEMA %I FROM %I', :'schema', :'role') \gexec
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I',
              :'schema', :'role') \gexec
SELECT format('GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA %I TO %I',
              :'schema', :'role') \gexec
SELECT format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO %I', :'schema', :'role') \gexec
-- tables, séquences et fonctions des migrations à venir (créées par ce rôle propriétaire)
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE '
              'ON TABLES TO %I', :'schema', :'role') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT USAGE, SELECT, UPDATE '
              'ON SEQUENCES TO %I', :'schema', :'role') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT EXECUTE ON FUNCTIONS TO %I',
              :'schema', :'role') \gexec

DO $audit$
DECLARE
    r text := current_setting('ameesh.role');
    s text := current_setting('ameesh.schema');
    proprietaire text;
    ecarts text[] := '{}';
    n int;
BEGIN
    SELECT pg_get_userbyid(nspowner) INTO proprietaire FROM pg_namespace WHERE nspname = s;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r
               AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls)) THEN
        ecarts := ecarts || 'attribut d''administration (superuser, createdb, createrole ou bypassrls)';
    END IF;
    IF has_schema_privilege(r, s, 'CREATE') THEN
        ecarts := ecarts || format('CREATE sur le schéma %s (reçu directement, par PUBLIC ou par héritage)', s);
    END IF;
    SELECT count(*) INTO n FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
     WHERE ns.nspname = s AND pg_has_role(r, c.relowner, 'USAGE');
    IF n > 0 THEN
        ecarts := ecarts || format('%s objet(s) du schéma possédés par le rôle ou un rôle dont il hérite', n);
    END IF;
    IF proprietaire IS NOT NULL AND pg_has_role(r, proprietaire, 'MEMBER') THEN
        ecarts := ecarts || format('membre du propriétaire du schéma (%s)', proprietaire);
    END IF;
    IF NOT has_table_privilege(r, format('%I.agent_registry', s), 'SELECT,INSERT,UPDATE,DELETE') THEN
        ecarts := ecarts || 'pas d''accès en écriture au registre (agent_registry)';
    END IF;
    IF array_length(ecarts, 1) > 0 THEN
        RAISE EXCEPTION 'CONTRAT DU RÔLE % NON TENU : %', r, array_to_string(ecarts, ' ; ');
    END IF;
END
$audit$;
COMMIT;
