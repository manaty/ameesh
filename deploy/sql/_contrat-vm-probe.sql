-- SPDX-License-Identifier: AGPL-3.0-only
-- #6298 audit seul OU inclus dans la transaction de role-vm-probe.sql.
-- Chaque relation/colonne autorisée est ci-dessous ; aucune révocation ici.
-- Exécuter en administrateur avec ON_ERROR_STOP et le search_path du mesh.
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role vm_probe
\endif
\if :{?writer}
\else
  \set writer vm_probe_writer
\endif
SELECT set_config('ameesh.vm_probe_role', :'role', false),
       set_config('ameesh.vm_probe_writer', :'writer', false) \g /dev/null
DO $audit$
DECLARE
    r text := current_setting('ameesh.vm_probe_role');
    w text := current_setting('ameesh.vm_probe_writer');
    s text := current_schema();
    n oid;
    who text;
    rid oid;
    allowed boolean;
    actual boolean;
    seq oid;
    fn oid;
    receipt_fn oid;
    item record;
    errors text[] := '{}';
BEGIN
    n := (SELECT oid FROM pg_namespace WHERE nspname=s);
    fn := to_regprocedure(format('%I.vm_probe_send(text,text,bigint,bigint,text)',s));
    receipt_fn := to_regprocedure(format('%I.vm_probe_receipt(text,text,bigint,bigint,text,bigint)',s));
    seq := pg_get_serial_sequence(format('%I.agent_mailbox',s),'id')::regclass;
    IF r=w OR n IS NULL OR fn IS NULL OR receipt_fn IS NULL THEN RAISE EXCEPTION 'vm_probe installation missing'; END IF;
    CREATE TEMP TABLE vm_probe_columns(subject text,rel text,cols text[]) ON COMMIT DROP;
    INSERT INTO pg_temp.vm_probe_columns VALUES
      (r,'host_lifecycle',ARRAY['host','mode','claims_open','generation','release_epoch','return_probe']),
      (w,'host_lifecycle',ARRAY['host','mode','claims_open','generation','release_epoch','return_probe']),
      (r,'agent_registry',ARRAY['name','host','mode','retired_at','handoff_fenced','runner_seen_host','runner_seen_owner','runner_seen_at','lease_owner','lease_expires_at','status','last_error']),
      (w,'agent_registry',ARRAY['name','aliases','host','mode','retired_at','handoff_fenced']),
      (r,'vm_adapter_requests',ARRAY['host','generation','release_epoch','state','status','result','updated_at']),
      (w,'vm_adapter_requests',ARRAY['host','generation','release_epoch','state','status','result','updated_at']),
      (w,'agent_mailbox',ARRAY['id','sender','recipient','body','host','created_at','delivered_at','meta','payload']),
      (w,'turn_resources',ARRAY['turn_id','ended_at']),
      (r,'turn_resources',ARRAY['turn_id','agent','host','status','started_at','ended_at']),
      (r,'host_lifecycle_agents',ARRAY['host','generation','agent']),
      (r,'host_handoffs',ARRAY['source','target','state']);
    FOR item IN SELECT c.subject AS who,c.rel,col FROM pg_temp.vm_probe_columns c,
        LATERAL unnest(c.cols) col WHERE NOT EXISTS
        (SELECT FROM pg_class k JOIN pg_attribute a ON a.attrelid=k.oid
         WHERE k.relnamespace=n AND k.relname=c.rel AND a.attname=col
           AND a.attnum>0 AND NOT a.attisdropped)
    LOOP errors := errors || format('missing column %s.%s (%s)',item.rel,item.col,item.who); END LOOP;
    FOREACH who IN ARRAY ARRAY[r,w] LOOP
        SELECT oid INTO rid FROM pg_roles WHERE rolname=who;
        IF rid IS NULL THEN RAISE EXCEPTION 'missing role %',who; END IF;
        IF EXISTS (SELECT FROM pg_roles WHERE oid=rid AND
            (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
             OR rolinherit OR rolcanlogin IS DISTINCT FROM (who=r))) THEN
            errors := errors || format('role attributes %s',who);
        END IF;
        IF EXISTS (SELECT FROM pg_auth_members WHERE member=rid OR roleid=rid) THEN
            errors := errors || format('role membership %s',who);
        END IF;
        IF EXISTS (SELECT FROM pg_authid WHERE oid=rid AND rolpassword IS NOT NULL
                   AND (who=w OR rolpassword NOT LIKE 'SCRAM-SHA-256$%')) THEN
            errors := errors || format('password is not dedicated SCRAM %s',who);
        END IF;
        -- Ownership confers implicit rights, including ALTER/DROP/GRANT.
        IF EXISTS (SELECT FROM pg_class WHERE relowner=rid)
           OR EXISTS (SELECT FROM pg_namespace WHERE nspowner=rid)
           OR EXISTS (SELECT FROM pg_database WHERE datdba=rid)
           OR EXISTS (SELECT FROM pg_proc WHERE proowner=rid AND (who<>w OR oid NOT IN (fn,receipt_fn)))
           OR EXISTS (SELECT FROM pg_type WHERE typowner=rid)
           OR EXISTS (SELECT FROM pg_largeobject_metadata WHERE lomowner=rid) THEN
            errors := errors || format('unexpected ownership %s',who);
        END IF;
        FOR item IN
          SELECT k.oid,k.relname,a.attname,a.attnum,k.relnamespace,nsp.nspname
          FROM pg_class k JOIN pg_namespace nsp ON nsp.oid=k.relnamespace
          JOIN pg_attribute a ON a.attrelid=k.oid AND a.attnum>0 AND NOT a.attisdropped
          WHERE k.relkind IN ('r','v','m','p','f')
            AND nsp.nspname NOT IN ('pg_catalog','information_schema')
            AND nsp.nspname !~ '^pg_(toast|temp_|toast_temp_)'
            -- Extension views can grant PUBLIC SELECT while their schema
            -- stays closed. Ignore that inert grant, but retain checks of
            -- direct grants to the role, even without namespace USAGE.
            AND (k.relnamespace=n OR has_schema_privilege(who,nsp.oid,'USAGE')
                 OR EXISTS (SELECT FROM aclexplode(k.relacl) x WHERE x.grantee=rid)
                 OR EXISTS (SELECT FROM aclexplode(a.attacl) x WHERE x.grantee=rid))
        LOOP
          allowed := item.relnamespace=n AND EXISTS
            (SELECT FROM pg_temp.vm_probe_columns c WHERE c.subject=who
             AND c.rel=item.relname AND item.attname=ANY(c.cols));
          actual := has_column_privilege(who,item.oid,item.attnum,'SELECT');
          IF actual IS DISTINCT FROM allowed THEN
            errors := errors || format('SELECT outside/missing contract %s %s.%s.%s',who,item.nspname,item.relname,item.attname);
          END IF;
        END LOOP;
        -- New PostgreSQL privilege kinds (e.g. MAINTAIN in 17) must not
        -- escape the explicit checks below; unknown grants are refused.
        IF EXISTS (SELECT FROM pg_class k CROSS JOIN LATERAL aclexplode(k.relacl) x
            WHERE k.relkind IN ('r','v','m','p','f')
              AND x.grantee IN (0,rid) AND x.privilege_type NOT IN
              ('SELECT','INSERT','UPDATE','REFERENCES','DELETE','TRUNCATE','TRIGGER')) THEN
            errors := errors || format('unlisted relation privilege %s',who);
        END IF;
        IF EXISTS (SELECT FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) x
            WHERE x.grantee=rid AND (x.privilege_type<>'CONNECT' OR d.datname<>current_database())) THEN
            errors := errors || format('database grant outside contract %s',who);
        END IF;
        -- Write privileges are column-bounded, including REFERENCES.
        FOR item IN
          SELECT k.oid,k.relname,k.relnamespace,a.attname,a.attnum,p.priv
          FROM pg_class k JOIN pg_namespace ns ON ns.oid=k.relnamespace
          JOIN pg_attribute a ON a.attrelid=k.oid AND a.attnum>0 AND NOT a.attisdropped
          CROSS JOIN unnest(ARRAY['INSERT','UPDATE','REFERENCES']) p(priv)
          WHERE k.relkind IN ('r','v','m','p','f')
            AND ns.nspname NOT IN ('pg_catalog','information_schema')
            AND ns.nspname !~ '^pg_(toast|temp_|toast_temp_)'
        LOOP
          allowed := who=w AND item.relnamespace=n AND (
            (item.priv='UPDATE' AND ((item.relname='host_lifecycle' AND item.attname='host')
              OR (item.relname='agent_registry' AND item.attname='name')))
            OR (item.priv='INSERT' AND item.relname='agent_mailbox' AND item.attname=ANY
              (ARRAY['sender','recipient','body','host','kind','payload','created_at'])));
          IF has_column_privilege(who,item.oid,item.attnum,item.priv) IS DISTINCT FROM allowed THEN
            errors := errors || format('%s outside/missing contract %s %s.%s',item.priv,who,item.relname,item.attname);
          END IF;
        END LOOP;
        FOR item IN SELECT k.oid,k.relname,p.priv FROM pg_class k
          JOIN pg_namespace ns ON ns.oid=k.relnamespace
          CROSS JOIN unnest(ARRAY['DELETE','TRUNCATE','TRIGGER']) p(priv)
          WHERE k.relkind IN ('r','v','m','p','f')
            AND ns.nspname NOT IN ('pg_catalog','information_schema')
            AND ns.nspname !~ '^pg_(toast|temp_|toast_temp_)'
            AND has_table_privilege(who,k.oid,p.priv)
        LOOP errors := errors || format('%s on %s (%s)',item.priv,item.relname,who); END LOOP;
        FOR item IN SELECT k.oid,k.relname,p.priv FROM pg_class k
          JOIN pg_namespace ns ON ns.oid=k.relnamespace
          CROSS JOIN unnest(ARRAY['USAGE','SELECT','UPDATE']) p(priv)
          WHERE k.relkind='S' AND ns.nspname !~ '^pg_'
        LOOP
          allowed := who=w AND item.oid=seq AND item.priv='USAGE';
          IF has_sequence_privilege(who,item.oid,item.priv) IS DISTINCT FROM allowed THEN
            errors := errors || format('sequence %s %s (%s)',item.relname,item.priv,who);
          END IF;
        END LOOP;
        FOR item IN SELECT p.oid,p.oid::regprocedure AS name FROM pg_proc p
          JOIN pg_namespace ns ON ns.oid=p.pronamespace
          WHERE ns.nspname NOT IN ('pg_catalog','information_schema')
            AND ns.nspname !~ '^pg_(toast|temp_|toast_temp_)'
            AND (p.prosecdef OR EXISTS(SELECT FROM aclexplode(p.proacl) x WHERE x.grantee=rid))
        LOOP
          IF has_function_privilege(who,item.oid,'EXECUTE') IS DISTINCT FROM (item.oid IN (fn,receipt_fn)) THEN
            errors := errors || format('function outside/missing contract %s (%s)',item.name,who);
          END IF;
        END LOOP;
        FOR item IN SELECT * FROM pg_namespace WHERE nspname NOT IN ('pg_catalog','information_schema')
          AND nspname !~ '^pg_(toast|temp_|toast_temp_)'
        LOOP
          IF has_schema_privilege(who,item.oid,'CREATE') OR
             (item.oid=n AND NOT has_schema_privilege(who,item.oid,'USAGE')) OR
             (item.oid<>n AND item.nspname<>'public' AND has_schema_privilege(who,item.oid,'USAGE')) THEN
            errors := errors || format('schema rights %s (%s)',item.nspname,who);
          END IF;
        END LOOP;
        FOR item IN SELECT datname FROM pg_database WHERE has_database_privilege(who,oid,'CREATE')
        LOOP errors := errors || format('CREATE database %s (%s)',item.datname,who); END LOOP;
        FOR item IN SELECT d.defaclobjtype FROM pg_default_acl d
          CROSS JOIN LATERAL aclexplode(d.defaclacl) x
          WHERE x.grantee<>d.defaclrole AND (x.grantee=rid OR
            (x.grantee=0 AND (d.defaclnamespace=0 OR has_schema_privilege(who,d.defaclnamespace,'USAGE'))))
        LOOP errors := errors || format('default ACL %s (%s)',item.defaclobjtype,who); END LOOP;
        IF current_setting('lo_compat_privileges')::boolean OR EXISTS
          (SELECT FROM pg_largeobject_metadata m CROSS JOIN LATERAL aclexplode(m.lomacl) x
           WHERE x.grantee IN (0,rid)) THEN errors := errors || format('large object rights %s',who); END IF;
        IF EXISTS (SELECT FROM pg_class k CROSS JOIN LATERAL aclexplode(k.relacl) x
                   WHERE x.grantee=rid AND x.is_grantable)
           OR EXISTS (SELECT FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) x
                      WHERE x.grantee=rid AND x.is_grantable) THEN
            errors := errors || format('grant option %s',who);
        END IF;
    END LOOP;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname=r AND 'default_transaction_read_only=on'=ANY(rolconfig))
       OR EXISTS (SELECT FROM pg_db_role_setting WHERE setrole IN (SELECT oid FROM pg_roles WHERE rolname IN (r,w))
                  AND setdatabase<>0) THEN errors := array_append(errors, 'unexpected per-database/read-only role setting'); END IF;
    -- A matching effective privilege must not conceal PUBLIC or delegation.
    IF EXISTS (SELECT FROM pg_class k JOIN pg_namespace ns ON ns.oid=k.relnamespace
        CROSS JOIN LATERAL aclexplode(k.relacl) x
        WHERE ns.nspname NOT IN ('pg_catalog','information_schema') AND ns.nspname !~ '^pg_'
          AND x.grantee=0
          AND (has_schema_privilege(r,ns.oid,'USAGE') OR has_schema_privilege(w,ns.oid,'USAGE')))
       OR EXISTS (SELECT FROM pg_attribute a JOIN pg_class k ON k.oid=a.attrelid
        JOIN pg_namespace ns ON ns.oid=k.relnamespace CROSS JOIN LATERAL aclexplode(a.attacl) x
        WHERE ns.nspname NOT IN ('pg_catalog','information_schema') AND ns.nspname !~ '^pg_'
          AND x.grantee=0
          AND (has_schema_privilege(r,ns.oid,'USAGE') OR has_schema_privilege(w,ns.oid,'USAGE')))
       THEN errors := array_append(errors, 'PUBLIC relation/column ACL'); END IF;
    IF EXISTS (SELECT FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) x
        WHERE x.grantee=(SELECT oid FROM pg_roles WHERE rolname=r) AND x.is_grantable)
       OR EXISTS (SELECT FROM pg_namespace ns CROSS JOIN LATERAL aclexplode(ns.nspacl) x
        WHERE x.grantee IN (SELECT oid FROM pg_roles WHERE rolname IN (r,w)) AND x.is_grantable)
       OR EXISTS (SELECT FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) x
        WHERE x.grantee IN (SELECT oid FROM pg_roles WHERE rolname IN (r,w)) AND x.is_grantable) THEN
        errors := array_append(errors, 'function/schema/database grant option');
    END IF;
    IF NOT EXISTS (SELECT FROM pg_proc WHERE oid=fn AND proowner=(SELECT oid FROM pg_roles WHERE rolname=w)
        AND prosecdef AND provolatile='v' AND NOT proleakproof
        AND proconfig=ARRAY['search_path=pg_catalog, pg_temp']
        -- Empreinte du corps relu, après normalisation du nom de schéma.
        AND md5(replace(prosrc,quote_ident(s)||'.','<mesh>.'))='39bc2409f257cb43947557696806b393')
       OR EXISTS (SELECT FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) x WHERE p.oid=fn AND x.grantee=0 AND x.privilege_type='EXECUTE') THEN
        errors := array_append(errors, 'bounded definer changed or PUBLIC executable');
    END IF;
    IF NOT EXISTS (SELECT FROM pg_proc WHERE oid=receipt_fn
        AND proowner=(SELECT oid FROM pg_roles WHERE rolname=w)
        AND prosecdef AND provolatile='v' AND NOT proleakproof
        AND proconfig=ARRAY['search_path=pg_catalog, pg_temp']
        AND md5(replace(prosrc,quote_ident(s)||'.','<mesh>.'))='4e5eff523cc99628d2866fea5684c390')
       OR EXISTS (SELECT FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) x
         WHERE p.oid=receipt_fn AND (x.grantee=0 OR x.is_grantable) AND x.privilege_type='EXECUTE') THEN
        errors := array_append(errors, 'bounded receipt definer changed or PUBLIC executable');
    END IF;
    -- #6329: only this enrolled login is constrained. Other mesh service
    -- roles (auditor, release controller) have their own privacy contracts.
    -- Include every role the login can SET ROLE to, even with NOINHERIT.
    IF has_table_privilege(r,format('%I.agent_mailbox',s),'SELECT') OR EXISTS (
      SELECT FROM pg_roles usable
      WHERE (usable.rolname=r OR pg_has_role(r,usable.oid,'MEMBER'))
        AND EXISTS (SELECT FROM pg_attribute a
          WHERE a.attrelid=format('%I.agent_mailbox',s)::regclass
            AND a.attnum>0 AND NOT a.attisdropped
            AND has_column_privilege(usable.oid,a.attrelid,a.attnum,'SELECT'))
    ) THEN
        errors := array_append(errors, 'vm_probe can read mailbox directly or via SET ROLE');
    END IF;
    IF cardinality(errors)>0 THEN RAISE EXCEPTION E'vm_probe contract refused:\n%',array_to_string(errors,E'\n'); END IF;
    RAISE NOTICE 'vm_probe contract held';
END
$audit$;
