-- SPDX-License-Identifier: AGPL-3.0-only
-- #6298 / manaty/ameesh#4858 : identité DB du compte VM, jamais le rôle ameesh.
-- Installation BUREAU uniquement, superutilisateur PostgreSQL (audit pg_authid) :
--   PGOPTIONS='-c search_path=<schema mesh>' psql <DSN admin> -X \
--     -v ON_ERROR_STOP=1 -f deploy/sql/role-vm-probe.sql
-- Le script est transactionnel, idempotent et refuse les droits étrangers,
-- PUBLIC, appartenances et ACL futures ; il ne les répare pas à leur source.
-- Audit seul (après chaque migration additive) : même commande sur
-- _contrat-vm-probe.sql. Variables role/writer réservées aux bancs isolés.
--
-- LOGIN vm_probe sans mot de passe initial : le bureau fixe le secret avec
-- \password vm_probe après SET password_encryption='scram-sha-256'. Jamais
-- d'argument contenant le secret. Pas de default_transaction_read_only :
-- il bloquerait aussi la fonction d'envoi (périmètre pilote du 08/11 07:53).
-- Les ACL ferment toute écriture directe ; seul le dépôt borné est permis.
-- Connexion : host=10.77.0.1 port=5432 sslmode=require user=vm_probe,
-- dbname et schéma réels du mesh, passfile dédié (aucun DSN/secret d'agent).
-- JSON de connexion root:vm-operator 0640, parents root non inscriptibles,
-- sans lien ; passfile libpq 0600 détenu par vm-operator dans son répertoire
-- privé 0700 (libpq ignore un passfile accessible au groupe). Aucun secret ici.
-- Le rôle propriétaire vm_probe_writer est NOLOGIN, sans membres : seul le
-- dépôt fixe peut faire INSERT ; le reçu borné ne rend aucun contenu.
-- vm_probe n’a aucun SELECT sur le courrier, ni INSERT ni séquence.
-- Aucun accès au schéma diagnostics. Aucune pose par l'auteur de ce lot.
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role vm_probe
\endif
\if :{?writer}
\else
  \set writer vm_probe_writer
\endif
BEGIN;
SELECT set_config('ameesh.vm_probe_role', :'role', true),
       set_config('ameesh.vm_probe_writer', :'writer', true) \g /dev/null
DO $install$
DECLARE
    r text := current_setting('ameesh.vm_probe_role');
    w text := current_setting('ameesh.vm_probe_writer');
    s text := current_schema();
    mailbox_columns text;
BEGIN
    IF r = w OR s IS NULL OR s IN ('pg_catalog','information_schema','diagnostics') THEN
        RAISE EXCEPTION 'invalid vm_probe enrollment';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname=r) THEN
        EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',r);
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname=w) THEN
        EXECUTE format('CREATE ROLE %I NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',w);
    END IF;
    IF has_schema_privilege(w,s,'CREATE') THEN RAISE EXCEPTION 'unexpected writer CREATE'; END IF;
    EXECUTE format('ALTER ROLE %I SET password_encryption=''scram-sha-256''',r);
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I, %I',s,r,w);
    -- Faits EXACTS du helper #6276 (pas SELECT * ; toute nouvelle colonne
    -- reste fermée). La fonction propriétaire utilise un sous-ensemble.
    EXECUTE format('GRANT SELECT (host,mode,claims_open,generation,release_epoch,return_probe) ON %I.host_lifecycle TO %I, %I',s,r,w);
    EXECUTE format('GRANT SELECT (name,host,mode,retired_at,handoff_fenced,runner_seen_host,runner_seen_owner,runner_seen_at,lease_owner,lease_expires_at,status,last_error) ON %I.agent_registry TO %I',s,r);
    EXECUTE format('GRANT SELECT (name,aliases,host,mode,retired_at,handoff_fenced) ON %I.agent_registry TO %I',s,w);
    EXECUTE format('GRANT SELECT (host,generation,release_epoch,state,status,result,updated_at) ON %I.vm_adapter_requests TO %I, %I',s,r,w);
    -- #6329: explicitly remove the old table AND every column SELECT grant.
    -- REVOKE at table level alone leaves the N-1 column ACLs effective.
    SELECT string_agg(quote_ident(attname),',') INTO mailbox_columns
      FROM pg_attribute WHERE attrelid=format('%I.agent_mailbox',s)::regclass
        AND attnum>0 AND NOT attisdropped;
    EXECUTE format('REVOKE SELECT ON %I.agent_mailbox FROM %I',s,r);
    EXECUTE format('REVOKE SELECT (%s) ON %I.agent_mailbox FROM %I',mailbox_columns,s,r);
    EXECUTE format('GRANT SELECT (id,sender,recipient,body,host,created_at,delivered_at,meta,payload) ON %I.agent_mailbox TO %I',s,w);
    EXECUTE format('GRANT SELECT (turn_id,ended_at) ON %I.turn_resources TO %I',s,w);
    EXECUTE format('GRANT SELECT (turn_id,agent,host,status,started_at,ended_at) ON %I.turn_resources TO %I',s,r);
    EXECUTE format('GRANT SELECT (host,generation,agent) ON %I.host_lifecycle_agents TO %I',s,r);
    EXECUTE format('GRANT SELECT (source,target,state) ON %I.host_handoffs TO %I',s,r);
    -- Row locks in the definer only. No UPDATE is exposed to the login.
    EXECUTE format('GRANT UPDATE (host) ON %I.host_lifecycle TO %I',s,w);
    EXECUTE format('GRANT UPDATE (name) ON %I.agent_registry TO %I',s,w);
    EXECUTE format('GRANT INSERT (sender,recipient,body,host,kind,payload,created_at) ON %I.agent_mailbox TO %I',s,w);
    EXECUTE format('GRANT USAGE ON SEQUENCE %s TO %I',pg_get_serial_sequence(format('%I.agent_mailbox',s),'id'),w);
    EXECUTE format($ddl$
CREATE OR REPLACE FUNCTION %1$I.vm_probe_send(p_host text,p_agent text,p_generation bigint,p_epoch bigint,p_nonce text)
RETURNS TABLE(request_id bigint,started_ts double precision)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,pg_temp
AS $fn$
DECLARE
    gate record;
    msg record;
    admitted timestamptz;
    key jsonb;
    fixed_body text;
BEGIN
    -- Native spelling triggers have their own search_path. A caller-created
    -- temporary relation must never shadow their registry reads.
    IF pg_my_temp_schema() <> 0 THEN RAISE EXCEPTION 'VM probe requires a clean session'; END IF;
    IF p_host IS NULL OR p_agent IS NULL OR p_nonce IS NULL
       OR p_host !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
       OR p_agent !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
       OR p_nonce !~ '^[0-9a-f]{32}$'
       OR p_generation IS NULL OR p_generation < 1 OR p_epoch IS NULL OR p_epoch < 0 THEN
        RAISE EXCEPTION 'invalid VM probe identity';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('host-gate:' || p_host,0));
    SELECT host,mode,claims_open,generation,release_epoch,return_probe INTO gate
      FROM %1$I.host_lifecycle WHERE host=p_host FOR SHARE;
    IF NOT FOUND OR gate.mode <> 'returning' OR gate.claims_open
       OR gate.generation <> p_generation OR gate.release_epoch <> p_epoch
       OR gate.return_probe IS DISTINCT FROM p_agent THEN
        RAISE EXCEPTION 'VM probe not enrolled in closed return';
    END IF;
    PERFORM name FROM %1$I.agent_registry WHERE name=p_agent AND host=p_host
      AND mode='execute' AND retired_at IS NULL AND NOT handoff_fenced FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'VM probe identity unavailable'; END IF;
    SELECT updated_at INTO admitted FROM %1$I.vm_adapter_requests
      WHERE host=p_host AND generation=p_generation AND release_epoch=p_epoch
        AND state='busy' AND status='pending' AND result='probe admitted';
    IF NOT FOUND THEN RAISE EXCEPTION 'VM probe not admitted'; END IF;
    key := jsonb_build_object('host',p_host,'probe_agent',p_agent,
      'generation',p_generation,'release_epoch',p_epoch,'nonce',p_nonce);
    fixed_body := 'VM return health probe. Acknowledge this nonce with agent-mail send '
      || p_agent || ' --ack "VM-PROBE-ACK ' || p_nonce
      || '" then end this turn. No other work is requested.';
    SELECT id,created_at,body,payload,host INTO msg FROM %1$I.agent_mailbox
      WHERE sender='vm-adapter' AND recipient=p_agent
        AND payload->>'idempotency_key'='vm-probe-' || p_nonce;
    IF FOUND THEN
        IF msg.payload->'vm_probe' IS DISTINCT FROM key OR msg.body <> fixed_body
           OR msg.host IS DISTINCT FROM p_host OR msg.created_at < admitted THEN
            RAISE EXCEPTION 'VM probe nonce reused';
        END IF;
        RETURN QUERY SELECT msg.id,extract(epoch FROM msg.created_at)::double precision;
        RETURN;
    END IF;
    -- One request per admission, even with a fresh caller-selected nonce.
    IF EXISTS (SELECT FROM %1$I.agent_mailbox WHERE sender='vm-adapter'
       AND recipient=p_agent AND host=p_host AND created_at >= admitted
       AND payload->'vm_probe'->>'generation'=p_generation::text
       AND payload->'vm_probe'->>'release_epoch'=p_epoch::text) THEN
        RAISE EXCEPTION 'VM probe already sent for this admission';
    END IF;
    INSERT INTO %1$I.agent_mailbox(sender,recipient,body,host,kind,payload,created_at)
      VALUES ('vm-adapter',p_agent,fixed_body,p_host,'request',
        jsonb_build_object('vm_probe',key,'idempotency_key','vm-probe-' || p_nonce),clock_timestamp())
      RETURNING id,created_at,recipient,sender,body,payload,host INTO msg;
    IF msg.recipient <> p_agent OR msg.sender <> 'vm-adapter' OR msg.host <> p_host
       OR msg.body <> fixed_body OR msg.payload->'vm_probe' IS DISTINCT FROM key THEN
        RAISE EXCEPTION 'VM probe deposit changed';
    END IF;
    RETURN QUERY SELECT msg.id,extract(epoch FROM msg.created_at)::double precision;
END
$fn$;
$ddl$,s);
    EXECUTE format('REVOKE ALL ON FUNCTION %I.vm_probe_send(text,text,bigint,bigint,text) FROM PUBLIC',s);
    EXECUTE format('GRANT CREATE ON SCHEMA %I TO %I',s,w);
    EXECUTE format('ALTER FUNCTION %I.vm_probe_send(text,text,bigint,bigint,text) OWNER TO %I',s,w);
    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM %I',s,w);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %I.vm_probe_send(text,text,bigint,bigint,text) TO %I',s,r);
    EXECUTE format($ddl$
CREATE OR REPLACE FUNCTION %1$I.vm_probe_receipt(p_host text,p_agent text,p_generation bigint,p_epoch bigint,p_nonce text,p_request bigint)
RETURNS TABLE(delivered boolean,turn_id text,acknowledged boolean)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,pg_temp
AS $fn$
DECLARE
    key jsonb;
BEGIN
    IF p_host IS NULL OR p_agent IS NULL OR p_nonce IS NULL OR p_request IS NULL OR p_request < 1
       OR p_host !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
       OR p_agent !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
       OR p_nonce !~ '^[0-9a-f]{32}$'
       OR p_generation IS NULL OR p_generation < 1 OR p_epoch IS NULL OR p_epoch < 0 THEN
        RETURN;
    END IF;
    key := jsonb_build_object('host',p_host,'probe_agent',p_agent,
      'generation',p_generation,'release_epoch',p_epoch,'nonce',p_nonce);
    RETURN QUERY
    SELECT m.delivered_at IS NOT NULL,
           m.meta->'consigne'->>'turn',
           EXISTS(SELECT FROM %1$I.agent_mailbox a
             WHERE m.delivered_at IS NOT NULL AND a.sender=p_agent AND a.recipient=p_agent
               AND a.host=p_host AND a.body='VM-PROBE-ACK ' || p_nonce
               AND a.payload @> '{"ack":true}'::jsonb
               AND a.created_at >= m.delivered_at
               AND a.created_at <= coalesce(t.ended_at,clock_timestamp()))
      FROM %1$I.agent_mailbox m
      JOIN %1$I.host_lifecycle g ON g.host=p_host AND g.mode='returning'
        AND NOT g.claims_open AND g.generation=p_generation
        AND g.release_epoch=p_epoch AND g.return_probe=p_agent
      JOIN %1$I.vm_adapter_requests q ON q.host=p_host AND q.generation=p_generation
        AND q.release_epoch=p_epoch AND q.state='busy' AND q.status='pending'
        AND q.result='probe admitted'
      LEFT JOIN %1$I.turn_resources t ON t.turn_id=m.meta->'consigne'->>'turn'
      WHERE m.id=p_request AND m.sender='vm-adapter' AND m.recipient=p_agent
        AND m.host=p_host AND m.payload->'vm_probe'=key
        AND m.payload->>'idempotency_key'='vm-probe-' || p_nonce
        AND NOT (m.meta ? 'retire') AND m.created_at >= q.updated_at;
END
$fn$;
$ddl$,s);
    EXECUTE format('REVOKE ALL ON FUNCTION %I.vm_probe_receipt(text,text,bigint,bigint,text,bigint) FROM PUBLIC',s);
    EXECUTE format('GRANT CREATE ON SCHEMA %I TO %I',s,w);
    EXECUTE format('ALTER FUNCTION %I.vm_probe_receipt(text,text,bigint,bigint,text,bigint) OWNER TO %I',s,w);
    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM %I',s,w);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %I.vm_probe_receipt(text,text,bigint,bigint,text,bigint) TO %I',s,r);
END
$install$;
\ir _contrat-vm-probe.sql
COMMIT;
