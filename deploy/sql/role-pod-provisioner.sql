-- SPDX-License-Identifier: AGPL-3.0-only
-- #7697 / verdict #44561 : identités DB distinctes pour le pourvoyeur de pods
-- enfants et pour le webhook d'admission.
--
-- Installation par un superutilisateur PostgreSQL, sur le schéma du mesh :
--   PGOPTIONS='-c search_path=<schéma mesh>' psql <DSN admin> -X \
--     -v ON_ERROR_STOP=1 -f deploy/sql/role-pod-provisioner.sql
--   PGOPTIONS='-c search_path=<schéma mesh>' psql <DSN admin> -X \
--     -v ON_ERROR_STOP=1 -f deploy/sql/_contrat-pod-provisioner.sql   # audit
--
-- Le script est transactionnel, idempotent et resserre : il révoque tous les
-- droits des deux rôles sur chaque table, vue et séquence du schéma, puis
-- n'accorde que le contrat ci-dessous. Un rôle préexistant aux droits plus
-- larges est donc ramené au contrat, jamais laissé tel quel.
--
-- Le pourvoyeur ne peut PAS modifier les champs d'autorisation d'une demande
-- (`tenant`, `enfant`, `Secret`, `expires_at`, etc.) : son UPDATE est borné
-- aux colonnes d'exploitation. Le webhook, lui, n'a que SELECT sur
-- `pod_requests` : sa source d'autorisation est en lecture seule au niveau
-- PostgreSQL, pas seulement par un drapeau applicatif (`writer=False`).
--
-- Les rôles sont LOGIN sans mot de passe initial : l'opérateur pose le secret
-- avec `\password <rôle>` (SCRAM, jamais d'argument contenant le secret).
-- La connexion se fait avec un passfile dédié 0600, jamais le DSN `ameesh`.
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role ameesh_pod_provisioner
\endif
\if :{?admission}
\else
  \set admission ameesh_pod_admission
\endif
BEGIN;
SELECT current_schema() AS schema \gset
SELECT set_config('ameesh.pod_role', :'role', true),
       set_config('ameesh.pod_admission', :'admission', true) \g /dev/null
DO $install$
DECLARE
    r text := current_setting('ameesh.pod_role');
    a text := current_setting('ameesh.pod_admission');
    s text := current_schema();
    item record;
BEGIN
    IF r = a OR s IS NULL OR s IN ('pg_catalog', 'information_schema', 'diagnostics') THEN
        RAISE EXCEPTION 'invalid pod role enrollment';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
        EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION NOBYPASSRLS', r);
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = a) THEN
        EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION NOBYPASSRLS', a);
    END IF;
    EXECUTE format('ALTER ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                   'NOCREATEROLE NOREPLICATION NOBYPASSRLS', r);
    EXECUTE format('ALTER ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                   'NOCREATEROLE NOREPLICATION NOBYPASSRLS', a);
    -- Aucune appartenance : un rôle membre hériterait des droits du parent
    -- (le rôle `ameesh` propriétaire, notamment).
    IF EXISTS (SELECT FROM pg_auth_members m JOIN pg_roles x ON x.oid = m.member
                WHERE x.rolname IN (r, a)) THEN
        RAISE EXCEPTION 'pod roles must not be members of another role';
    END IF;

    -- Repartir d'un ACL vide : un ancien grant large ne survit pas au script.
    FOR item IN
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
    LOOP
        EXECUTE format('REVOKE ALL ON %I.%I FROM %I, %I', s, item.relname, r, a);
    END LOOP;
    FOR item IN
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = s AND c.relkind = 'S'
    LOOP
        EXECUTE format('REVOKE ALL ON SEQUENCE %I.%I FROM %I, %I',
                       s, item.relname, r, a);
    END LOOP;
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I, %I', s, r, a);
    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM %I, %I', s, r, a);

    -- Le webhook ne lit que la demande, jamais autre chose.
    EXECUTE format('GRANT SELECT ON %I.pod_requests TO %I', s, a);

    -- Le pourvoyeur lit la demande et l'hôte enfant, et n'écrit que
    -- l'exploitation. Les colonnes d'autorisation restent hors de son ACL.
    EXECUTE format('GRANT SELECT ON %I.pod_requests TO %I', s, r);
    EXECUTE format('GRANT UPDATE (state, pod_name, claimed_by, claimed_at, '
                   'started_at, finished_at, error) ON %I.pod_requests TO %I', s, r);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I.pod_templates TO %I', s, r);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I.pod_hosts TO %I', s, r);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I.host_tenancy TO %I', s, r);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I.host_tenants TO %I', s, r);
    EXECUTE format('GRANT SELECT (name, host) ON %I.agent_registry TO %I', s, r);

    -- Ce que le pourvoyeur n'a jamais : créer ou supprimer une demande,
    -- changer un champ d'autorisation, écrire dans le registre.
    IF has_table_privilege(r, format('%I.pod_requests', s), 'INSERT')
       OR has_table_privilege(r, format('%I.pod_requests', s), 'DELETE')
       OR has_table_privilege(r, format('%I.pod_requests', s), 'TRUNCATE')
       OR has_table_privilege(r, format('%I.pod_requests', s), 'UPDATE') THEN
        RAISE EXCEPTION 'pod provisioner kept a table-level write on pod_requests';
    END IF;
    IF has_table_privilege(a, format('%I.pod_requests', s), 'INSERT')
       OR has_table_privilege(a, format('%I.pod_requests', s), 'UPDATE')
       OR has_table_privilege(a, format('%I.pod_requests', s), 'DELETE') THEN
        RAISE EXCEPTION 'pod admission role is not read-only';
    END IF;
END
$install$;
COMMIT;
