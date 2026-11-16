-- SPDX-License-Identifier: AGPL-3.0-only
-- Restore the guard from 0583; views and grants are unchanged.
set local lock_timeout = '5s';
create or replace function ameesh_limited_login_guard() returns trigger
language plpgsql
set search_path = pg_catalog, pg_temp
as $$
declare
    marker  oid := to_regrole('ameesh_login_limite');
    s       text := tg_table_schema;
    l       record;
    lot     record;
    bound   boolean;
    claim   jsonb;
    binding bigint;
begin
    if marker is null or not exists (
            select from pg_auth_members
             where roleid = marker and member = to_regrole(quote_ident(session_user))) then
        return case when tg_op = 'DELETE' then old else new end;
    end if;
    execute format('select agent, host from %I.limited_logins where rolname = $1', s)
       into l using session_user;
    if l.agent is null then
        raise exception 'limited login % is not enrolled in schema %', session_user, s
            using errcode = 'insufficient_privilege';
    end if;
    if tg_op = 'DELETE' then
        raise exception 'limited login %: no deletion on %', session_user, tg_table_name
            using errcode = 'insufficient_privilege';
    end if;

    if tg_table_name = 'session_bindings' then
        -- sa propre liaison active : la créer, changer son PID, la révoquer
        -- (`mail unbind`) ; jamais rouvrir une liaison révoquée
        if new.agent is distinct from l.agent or new.host is distinct from l.host
           or (tg_op = 'INSERT' and new.revoked_at is not null)
           or (tg_op = 'UPDATE' and (old.agent is distinct from l.agent
                                     or old.host is distinct from l.host
                                     or old.revoked_at is not null)) then
            raise exception 'limited login %: binds % on % only', session_user, l.agent, l.host
                using errcode = 'insufficient_privilege';
        end if;
    elsif tg_table_name = 'agent_mailbox' then
        if tg_op = 'UPDATE' and old.recipient = l.agent and old.sender is distinct from l.agent then
            -- courrier reçu (hook `agent-mail`) : réserver (`meta.consigne`)
            -- puis remettre, jamais rendre non lu un message remis ni toucher
            -- au reste de `meta`
            if (new.meta - 'consigne' - 'deja_consigne') is distinct from (old.meta - 'consigne' - 'deja_consigne')
               or (old.delivered_at is not null
                    and new.delivered_at is distinct from old.delivered_at)
               or (new.status is distinct from old.status
                    and (new.status is distinct from 'delivered' or new.delivered_at is null)) then
                raise exception 'limited login %: mail to % is reserved or delivered only', session_user, l.agent
                    using errcode = 'insufficient_privilege';
            end if;
        elsif new.sender is distinct from l.agent
           or (new.host is not null and new.host is distinct from l.host)
           or (tg_op = 'UPDATE' and (old.sender is distinct from l.agent
                                     or new.delivered_at is distinct from old.delivered_at
                                     or new.status is distinct from old.status)) then
            raise exception 'limited login %: mail from % only', session_user, l.agent
                using errcode = 'insufficient_privilege';
        end if;
    elsif tg_table_name = 'work_items' then
        -- #6839 : seule la prise du geste externe change, sur un lot réservé à
        -- cet agent ; une prise neuve ou gardée nomme une liaison L41 active
        -- de cet agent sur cet hôte ; jamais la prise d'une autre session.
        if old.external_executor is distinct from l.agent then
            raise exception 'limited login %: lot % is not reserved to %',
                session_user, old.id, l.agent using errcode = 'insufficient_privilege';
        end if;
        if new.external_claim_session is not null then
            execute format('select exists (select from %I.session_bindings'
                           ' where session_id = $1 and agent = $2 and host = $3'
                           ' and revoked_at is null)', s)
               into bound using new.external_claim_proof, l.agent, l.host;
            if (old.external_claim_session is not null
                    and old.external_claim_session is distinct from new.external_claim_session)
               or new.external_claim_proof is distinct from new.external_claim_session
               or new.external_claim_actor is distinct from l.agent
               or new.external_claim_host is distinct from l.host
               or not bound then
                raise exception 'limited login %: claim of lot % refused (bound session of % on % only)',
                    session_user, old.id, l.agent, l.host using errcode = 'insufficient_privilege';
            end if;
            -- #6877 (0566), lu par to_jsonb tant que la colonne peut manquer :
            -- le harnais et la ligne de liaison nommés vont ensemble et
            -- désignent la liaison active de cette session, de cet agent, sur
            -- cet hôte
            claim := to_jsonb(new);
            if claim ? 'external_claim_binding' then
                binding := nullif(claim->>'external_claim_binding', '')::bigint;
                if (binding is null) is distinct from (nullif(claim->>'external_claim_harness', '') is null) then
                    raise exception 'limited login %: claim of lot % names harness and binding together',
                        session_user, old.id using errcode = 'insufficient_privilege';
                end if;
                if binding is not null then
                    execute format('select exists (select from %I.session_bindings'
                                   ' where id = $1 and session_id = $2 and harness = $3'
                                   ' and agent = $4 and host = $5 and revoked_at is null)', s)
                       into bound using binding, new.external_claim_proof,
                                        claim->>'external_claim_harness', l.agent, l.host;
                    if not bound then
                        raise exception 'limited login %: claim of lot % names binding % that is not its active one',
                            session_user, old.id, binding using errcode = 'insufficient_privilege';
                    end if;
                end if;
            end if;
        elsif old.external_claim_session is not null
              and old.external_claim_host is distinct from l.host then
            raise exception 'limited login %: claim of lot % was taken on %',
                session_user, old.id, old.external_claim_host using errcode = 'insufficient_privilege';
        end if;
    elsif tg_table_name = 'work_item_events' then
        execute format('select state, external_executor from %I.work_items where id = $1', s)
           into lot using new.work_item_id;
        if new.actor is distinct from l.agent
           or lot.external_executor is distinct from l.agent
           or new.state is distinct from lot.state then
            raise exception 'limited login %: notes by % on its own external lots only',
                session_user, l.agent using errcode = 'insufficient_privilege';
        end if;
    elsif tg_table_name = 'provenance_receipts' then
        if new.actor is distinct from l.agent then
            raise exception 'limited login %: receipts by % only', session_user, l.agent
                using errcode = 'insufficient_privilege';
        end if;
    elsif tg_table_name = 'thread_index' then
        if new.host is distinct from l.host
           or (tg_op = 'UPDATE' and old.host is distinct from l.host) then
            raise exception 'limited login %: thread index of % only', session_user, l.host
                using errcode = 'insufficient_privilege';
        end if;
    elsif tg_table_name = 'agent_name_resolutions' then
        -- audit d'un alias résolu (0418) : signé par la connexion elle-même
        if tg_op <> 'INSERT' or new.db_user is distinct from session_user then
            raise exception 'limited login %: alias audit as itself only', session_user
                using errcode = 'insufficient_privilege';
        end if;
    elsif tg_table_name = 'ameesh_events' then
        -- Triggers, or a bounded catch-up declaration by the enrolled bureau.
        if pg_trigger_depth() < 2 and (
            tg_op = 'INSERT' and l.agent = 'bureau'
            and new.type = 'staging.catchup'
            and new.project is not null
            and new.object = 'staging-catchup:' || new.project || ':' || (new.data->>'host')
            and new.data->>'schema' = 'ameesh-staging-catchup/1'
            and new.data->>'actor' = l.agent
            and new.data->>'sha' ~ '^[0-9a-f]{40}$'
            and new.data->>'result' in ('ok', 'failed')
        ) is not true then
            raise exception 'limited login %: events come from triggers only', session_user
                using errcode = 'insufficient_privilege';
        end if;
    else
        -- agent_registry (le droit UPDATE n'y sert qu'au verrou de ligne) et
        -- toute autre table : aucune écriture
        raise exception 'limited login %: no write on %', session_user, tg_table_name
            using errcode = 'insufficient_privilege';
    end if;
    return new;
end $$;

