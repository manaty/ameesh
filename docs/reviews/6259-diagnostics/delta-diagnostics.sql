-- #6259 v2 — pg_stat_statements hors de public (décision mesh-design #200197, option b).
-- Base ameesh du central, rôle postgres, UNE transaction. Aucun lecteur observé -> aucun GRANT USAGE.
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
  -- avis impl-03 : un ACL par défaut de schéma donnerait USAGE au nouveau schéma
  IF EXISTS (SELECT 1 FROM pg_default_acl WHERE defaclobjtype = 'n') THEN
    RAISE EXCEPTION 'ACL par défaut de schéma présente (pg_default_acl n) : rien fait';
  END IF;
END $$;
CREATE SCHEMA diagnostics AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA diagnostics FROM PUBLIC;
ALTER EXTENSION pg_stat_statements SET SCHEMA diagnostics;
DO $$
DECLARE ext oid := (SELECT oid FROM pg_extension WHERE extname = 'pg_stat_statements');
        owner oid := (SELECT nspowner FROM pg_namespace WHERE nspname = 'diagnostics');
BEGIN
  IF (SELECT extnamespace FROM pg_extension WHERE oid = ext) <> 'diagnostics'::regnamespace
     OR to_regclass('public.pg_stat_statements') IS NOT NULL OR to_regclass('public.pg_stat_statements_info') IS NOT NULL THEN
    RAISE EXCEPTION 'extension non déplacée : transaction annulée';
  END IF;
  -- aucun droit sur le schéma hors de son propriétaire (PUBLIC compris)
  IF EXISTS (SELECT 1 FROM pg_namespace n CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) x
             WHERE n.nspname = 'diagnostics' AND x.grantee <> owner) THEN
    RAISE EXCEPTION 'droit sur diagnostics hors propriétaire : transaction annulée';
  END IF;
  -- aucun membre de l'extension resté dans public (relations, types, fonctions)
  IF EXISTS (SELECT 1 FROM pg_depend d
              LEFT JOIN pg_class c ON d.classid = 'pg_class'::regclass AND c.oid = d.objid
              LEFT JOIN pg_type t ON d.classid = 'pg_type'::regclass AND t.oid = d.objid
              LEFT JOIN pg_proc p ON d.classid = 'pg_proc'::regclass AND p.oid = d.objid
             WHERE d.refclassid = 'pg_extension'::regclass AND d.refobjid = ext AND d.deptype = 'e'
               AND 'public'::regnamespace IN (c.relnamespace, t.typnamespace, p.pronamespace)) THEN
    RAISE EXCEPTION 'membre d''extension resté dans public : transaction annulée';
  END IF;
END $$;
COMMIT;
