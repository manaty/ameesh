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
-- et ses écritures de liaison sont bornées par des triggers aux hôtes de ses
-- propres demandes et aux valeurs de la demande (`ameesh_pod_write_guard`),
-- jamais à un hôte VM partagé, une durée prolongée ou un autre tenant.
-- (`tenant`, `enfant`, `Secret`, `expires_at`, etc.) : son UPDATE est borné
-- aux colonnes d'exploitation. Le webhook, lui, n'a que SELECT sur
-- `pod_requests` : sa source d'autorisation est en lecture seule au niveau
-- PostgreSQL, pas seulement par un drapeau applicatif (`writer=False`).
--
-- Les rôles sont LOGIN sans mot de passe initial : l'opérateur pose le secret
-- avec `\password <rôle>` (SCRAM, jamais d'argument contenant le secret).
-- La connexion se fait avec un passfile dédié 0600, jamais le DSN `ameesh`.
--
-- #7986 : en mode rejeu (`ameesh.rejeu_pod` = '1', posé par
-- rejeu-pod-provisioner.sql sous le propriétaire des tables, sans
-- CREATEROLE), les CREATE/ALTER ROLE et le remplacement de la fonction de
-- garde sont sautés : les deux rôles et la fonction doivent déjà exister,
-- seuls les GRANT/REVOKE et les triggers sont rejoués, et l'audit
-- _contrat-pod-provisioner.sql est inclus dans la même transaction. Créer les
-- rôles, poser leurs attributs et remplacer la fonction restent les gestes de
-- l'administrateur.
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
    rejeu boolean := coalesce(current_setting('ameesh.rejeu_pod', true), '') = '1';
    item record;
BEGIN
    IF r = a OR s IS NULL OR s IN ('pg_catalog', 'information_schema', 'diagnostics') THEN
        RAISE EXCEPTION 'invalid pod role enrollment';
    END IF;
    IF NOT rejeu AND NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
        EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION NOBYPASSRLS', r);
    END IF;
    IF NOT rejeu AND NOT EXISTS (SELECT FROM pg_roles WHERE rolname = a) THEN
        EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION NOBYPASSRLS', a);
    END IF;
    IF NOT rejeu THEN
        EXECUTE format('ALTER ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION NOBYPASSRLS', r);
        EXECUTE format('ALTER ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION NOBYPASSRLS', a);
    END IF;
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
    -- `running` lit le host de l'enfant pour refuser un pod dont l'enfant a
    -- disparu ; deux colonnes suffisent.
    EXECUTE format('GRANT SELECT (name, host) ON %I.agent_registry TO %I', s, r);

    -- #44568/#44569/#44574 : les tables de liaison portent la politique de
    -- TOUS les hôtes, pods et VM. Le pourvoyeur n'écrit que pour les hôtes de
    -- ses propres demandes, et seulement avec les valeurs de la demande.
    -- Des triggers (et non une RLS : l'audit de sauvegarde #5162 refuse toute
    -- table sous RLS) bornent l'hôte et vérifient chaque champ autorisé.
    -- #7986 : le remplacement de la fonction appartient à l'administrateur
    -- (elle est possédée par un autre rôle) ; en rejeu, elle doit déjà exister.
    IF NOT rejeu THEN
    EXECUTE format($ddl$
        CREATE OR REPLACE FUNCTION %I.ameesh_pod_write_guard()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path = %I, pg_catalog, pg_temp
        AS $body$
        DECLARE
            req record;
        BEGIN
            -- Les autres rôles (propriétaire, sauvegarde, exécuteurs) gardent
            -- leurs écritures ; seul le rôle du pourvoyeur est borné.
            IF current_user <> %L THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF NOT EXISTS (SELECT 1 FROM pod_requests WHERE host = OLD.host) THEN
                    RAISE EXCEPTION 'pod provisioner write outside its pod request'
                        USING ERRCODE = '42501';
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP = 'UPDATE' AND NEW.host IS DISTINCT FROM OLD.host THEN
                RAISE EXCEPTION 'pod provisioner may not move a host row'
                    USING ERRCODE = '42501';
            END IF;
            SELECT * INTO req FROM pod_requests WHERE host = NEW.host;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'pod provisioner write outside its pod request'
                    USING ERRCODE = '42501';
            END IF;
            IF TG_TABLE_NAME = 'pod_hosts' THEN
                IF NEW.namespace IS DISTINCT FROM req.namespace
                   OR NEW.template IS DISTINCT FROM req.template
                   OR NEW.parent IS DISTINCT FROM req.parent
                   OR NEW.tenant IS DISTINCT FROM req.tenant
                   OR NEW.uid IS DISTINCT FROM req.uid
                   OR NEW.runner_id IS DISTINCT FROM req.runner_id
                   OR NEW.expires_at IS DISTINCT FROM req.expires_at THEN
                    RAISE EXCEPTION 'pod host fields differ from the pod request'
                        USING ERRCODE = '42501';
                END IF;
            ELSIF TG_TABLE_NAME = 'host_tenancy' THEN
                IF NEW.canon IS DISTINCT FROM 'pod:' || req.template
                   OR NEW.shared IS DISTINCT FROM true
                   OR NEW.tenants IS DISTINCT FROM ARRAY[req.tenant]
                   OR NEW.canon_ref IS DISTINCT FROM 'pod-request:' || req.id::text THEN
                    RAISE EXCEPTION 'host tenancy differs from the pod request'
                        USING ERRCODE = '42501';
                END IF;
            ELSIF TG_TABLE_NAME = 'host_tenants' THEN
                IF NEW.tenant IS DISTINCT FROM req.tenant
                   OR NEW.uid IS DISTINCT FROM req.uid
                   OR NEW.unix_user IS DISTINCT FROM req.unix_user
                   OR NEW.runner_id IS DISTINCT FROM req.runner_id THEN
                    RAISE EXCEPTION 'host tenant differs from the pod request'
                        USING ERRCODE = '42501';
                END IF;
            END IF;
            RETURN NEW;
        END
        $body$;
    $ddl$, s, s, r);
    END IF;
    FOR item IN
        SELECT unnest(ARRAY['pod_hosts', 'host_tenancy', 'host_tenants']) AS rel
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS pod_scope_guard ON %I.%I',
                       s, item.rel);
        EXECUTE format('CREATE TRIGGER pod_scope_guard BEFORE INSERT OR UPDATE '
                       'OR DELETE ON %I.%I FOR EACH ROW EXECUTE FUNCTION '
                       '%I.ameesh_pod_write_guard()', s, item.rel, s);
        -- Une RLS laissée par une version antérieure du script est retirée :
        -- l'audit #5162 refuse une relation sous RLS.
        EXECUTE format('DROP POLICY IF EXISTS pod_scope_open ON %I.%I', s, item.rel);
        EXECUTE format('DROP POLICY IF EXISTS pod_scope_provisioner ON %I.%I',
                       s, item.rel);
        EXECUTE format('ALTER TABLE %I.%I DISABLE ROW LEVEL SECURITY', s, item.rel);
    END LOOP;

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
-- #7986 : l'audit complet du contrat, dans la même transaction que
-- l'installation ou que le rejeu (le fichier est aussi utilisable seul).
\ir _contrat-pod-provisioner.sql
COMMIT;
