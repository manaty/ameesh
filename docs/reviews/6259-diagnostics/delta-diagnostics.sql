-- #6259 — pg_stat_statements hors de public (décision mesh-design #200197, option b).
-- Base ameesh du central, rôle postgres, UNE transaction. Lecteurs relevés : aucun -> aucun GRANT USAGE.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $$ BEGIN
  IF (SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'pg_stat_statements') IS DISTINCT FROM 'public' THEN
    RAISE EXCEPTION 'pg_stat_statements pas dans public : état inattendu, rien fait';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'diagnostics') THEN
    RAISE EXCEPTION 'schéma diagnostics déjà présent : rien fait';
  END IF;
END $$;
CREATE SCHEMA diagnostics AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA diagnostics FROM PUBLIC;
ALTER EXTENSION pg_stat_statements SET SCHEMA diagnostics;
DO $$ BEGIN
  IF (SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'pg_stat_statements') <> 'diagnostics'
     OR to_regclass('public.pg_stat_statements') IS NOT NULL OR to_regclass('public.pg_stat_statements_info') IS NOT NULL
     OR has_schema_privilege('public', 'diagnostics', 'USAGE') THEN
    RAISE EXCEPTION 'contrôle après déplacement en échec : transaction annulée';
  END IF;
END $$;
COMMIT;
