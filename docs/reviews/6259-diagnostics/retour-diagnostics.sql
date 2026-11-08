-- Retour #6259 v2 : remettre pg_stat_statements dans public et supprimer le schéma vide.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER EXTENSION pg_stat_statements SET SCHEMA public;
DROP SCHEMA diagnostics;   -- sans CASCADE : échoue s'il n'est pas vide
COMMIT;
