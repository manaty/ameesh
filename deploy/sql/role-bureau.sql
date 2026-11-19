-- SPDX-License-Identifier: AGPL-3.0-only
-- #6881 / manaty/ameesh#5236 : connexion PostgreSQL LIMITÉE du bureau depuis
-- bureau-goal (décision owner #211859 a), jamais le rôle ameesh ni un DSN
-- d'agent. Installation par l'ADMINISTRATEUR (CREATEROLE, propriétaire des
-- tables ou superutilisateur ; audit pg_authid) :
--   PGOPTIONS='-c search_path=<schéma du mesh>' psql <DSN admin> -X \
--     -v ON_ERROR_STOP=1 [-v role=bureau_goal] [-v agent=bureau] \
--     [-v host=bureau-goal] -f deploy/sql/role-bureau.sql
-- Audit seul (après chaque migration) : même commande sur _contrat-bureau.sql.
--
-- Ce que la connexion peut faire : lire les lots, le registre et le courrier ;
-- se lier (L41, `mail bind`) comme `agent` sur `host` ; prendre et lever un
-- geste externe réservé à `agent` (#6839) avec une liaison active, et lever
-- SA propre réservation d'exécutant (#7870) ; déposer
-- son courrier et ses reçus de provenance ; lire les seules attestations
-- staging.attested via ameesh_staging_attestations, et les preuves publiques
-- de décisions approuvées déjà consommées via ameesh_decision_proofs ; écrire
-- une trace staging.catchup bornée, datée par le serveur (0584). Les tables
-- sources restent fermées, sauf SELECT(id) sur ameesh_events pour RETURNING.
-- Tout le reste est fermé :
--   - droits : la liste exacte de _bureau-droits.sql (droits de colonne pour
--     toute écriture), rien d'autre, ni PUBLIC, ni appartenance, ni propriété ;
--   - lignes : la garde 0567 (`ameesh_limited_login_guard`) refuse toute
--     écriture pour un autre agent ou un autre hôte, la prise d'une autre
--     session, toute écriture du registre (son droit UPDATE ne sert qu'au
--     verrou de ligne), tout événement hors trigger sauf la trace staging.catchup du bureau ;
--   - aucun DDL (ni CREATE sur le schéma, ni propriété), aucun SECURITY
--     DEFINER exécutable, aucune table de secrets, de clés ni d'approbation.
-- Limite dite : la lecture du courrier n'est pas bornée au destinataire
-- (le CLI lit le dernier message de chaque agent) ; deux sessions du même
-- agent sur le même hôte partagent la connexion : la base ne les distingue
-- que par leur liaison L41 (PID vérifié localement par le CLI).
--
-- LOGIN sans mot de passe initial : l'administrateur pose le secret avec
-- \password <role> (après SET password_encryption='scram-sha-256'), jamais en
-- argument ; il est copié dans le .pgpass 0600 du compte du bureau
-- (deploy/bureau/installer-bureau-goal.sh). Aucun secret ici.
--
-- Transactionnel et idempotent : retire les droits DIRECTS de la connexion
-- sur le schéma, pose la liste, puis audite (_contrat-bureau.sql) ; un droit
-- étranger (PUBLIC, appartenance, ACL par défaut) fait échouer et annule tout.
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role bureau_goal
\endif
\if :{?agent}
\else
  \set agent bureau
\endif
\if :{?host}
\else
  \set host bureau-goal
\endif
BEGIN;
SELECT set_config('ameesh.bureau_role', :'role', true),
       set_config('ameesh.bureau_agent', :'agent', true),
       set_config('ameesh.bureau_host', :'host', true) \g /dev/null
\ir _bureau-droits.sql
DO $install$
DECLARE
    r text := current_setting('ameesh.bureau_role');
    a text := current_setting('ameesh.bureau_agent');
    h text := current_setting('ameesh.bureau_host');
    m text := 'ameesh_login_limite';
    s text := current_schema();
BEGIN
    IF current_setting('server_version_num')::int < 160000 THEN
        RAISE EXCEPTION 'PostgreSQL 16+ required (membership without SET ROLE)';
    END IF;
    IF s IS NULL OR s IN ('pg_catalog','information_schema','diagnostics')
       OR r = m OR btrim(r) = '' OR btrim(a) = '' OR btrim(h) = '' THEN
        RAISE EXCEPTION 'invalid bureau enrollment (schema %, role %)', s, r;
    END IF;
    IF to_regclass(format('%I.limited_logins', s)) IS NULL
       OR to_regprocedure(format('%I.ameesh_limited_login_guard()', s)) IS NULL THEN
        RAISE EXCEPTION 'migration 0567 (limited login guard) missing in schema %', s;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = m) THEN
        EXECUTE format('CREATE ROLE %I NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', m);
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
        EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', r);
    END IF;
    EXECUTE format('ALTER ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', r);
    EXECUTE format('ALTER ROLE %I SET password_encryption = ''scram-sha-256''', r);
    EXECUTE format('ALTER ROLE %I SET jit = off', r);
    -- le marqueur ne porte aucun droit : il désigne la connexion à la garde
    -- 0567 ; ni héritage ni SET ROLE (PG 16+)
    EXECUTE format('GRANT %I TO %I WITH INHERIT FALSE, SET FALSE', m, r);
    EXECUTE format('INSERT INTO %I.limited_logins(rolname, agent, host) VALUES ($1, $2, $3)'
                   ' ON CONFLICT (rolname) DO UPDATE SET agent = excluded.agent, host = excluded.host', s)
       USING r, a, h;
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', s, r);
    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM %I', s, r);
END
$install$;
-- droits DIRECTS remis à zéro, puis la liste exacte (#7873 : le même script
-- est rejoué par le staging après les migrations, rejeu-droits-limites.sql)
\ir _bureau-poser-droits.sql
\ir _contrat-bureau.sql
COMMIT;
