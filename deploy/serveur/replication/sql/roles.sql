-- SPDX-License-Identifier: AGPL-3.0-only
--
-- Rôles de la réplication en flux PostgreSQL 17 du mesh (lot #6868).
-- Rejouable : les rôles existants sont repris et durcis, jamais recréés.
-- Les mots de passe sont posés par configurer-noeud.sh sur l'entrée standard
-- de psql, après ce fichier : ils n'apparaissent ni ici, ni en argument.
--
--   replicator : LOGIN REPLICATION, pour pg_basebackup et le flux des standby ;
--   rewind     : LOGIN sans superuser, pour pg_rewind depuis un primaire en
--                ligne ; les quatre fonctions sont celles exigées par la
--                documentation PostgreSQL 17 (pg_rewind, Notes).
--
-- Aucun des deux n'est superuser, ne crée de base ni de rôle.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replicator') THEN
    CREATE ROLE replicator LOGIN REPLICATION NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  ELSE
    ALTER ROLE replicator LOGIN REPLICATION NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rewind') THEN
    CREATE ROLE rewind LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  ELSE
    ALTER ROLE rewind LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END
$$;

GRANT EXECUTE ON FUNCTION pg_catalog.pg_ls_dir(text, boolean, boolean) TO rewind;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_stat_file(text, boolean) TO rewind;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text) TO rewind;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text, bigint, bigint, boolean) TO rewind;
