-- Retour #6259 : remettre pg_stat_statements dans public et supprimer le schéma vide.
\set ON_ERROR_STOP on
BEGIN;
ALTER EXTENSION pg_stat_statements SET SCHEMA public;
DROP SCHEMA diagnostics;   -- échoue s'il n'est pas vide : rien d'autre ne doit y être
COMMIT;
