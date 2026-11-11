-- SPDX-License-Identifier: AGPL-3.0-only
-- 0567_limited_login_guard — #6881 : garde par ligne des connexions limitées
-- (bureau sur bureau-goal, décision owner #211859 a).
--
-- Une connexion limitée (deploy/sql/role-bureau.sql) n'a que des droits de
-- colonne. Ils ne suffisent pas : verrouiller une ligne (`FOR SHARE` du dépôt
-- de courrier, `FOR UPDATE` de la prise #6839) exige un droit UPDATE, et un
-- INSERT dans le courrier accepte n'importe quel expéditeur. Cette garde
-- borne les LIGNES : chaque connexion limitée est inscrite dans
-- `limited_logins` avec l'agent et l'hôte qu'elle porte ; elle n'écrit que
-- pour cet agent, depuis cet hôte, et ne prend que les gestes réservés à cet
-- agent, avec une liaison de session L41 active.
--
-- Qui est limité : un membre DIRECT du rôle marqueur `ameesh_login_limite`
-- (pg_auth_members, lisible par tous ; `session_user`, que SET ROLE ne
-- change pas). Sans ce rôle, ou pour tout autre utilisateur — superutilisateur
-- compris —, la garde rend la ligne inchangée : le code d'avant et les
-- exécuteurs ne voient aucune différence. Lectures qualifiées par le schéma
-- du trigger : une table temporaire de la session ne masque rien.
--
-- Additif : une table neuve, une fonction, des triggers BEFORE qui ne refusent
-- qu'une connexion limitée.
-- ameesh: trigger compatible with previous code
set local lock_timeout = '5s';

create table if not exists limited_logins (
    rolname    text primary key check (btrim(rolname) <> ''),
    agent      text not null check (btrim(agent) <> ''),
    host       text not null check (btrim(host) <> ''),
    created_at timestamptz not null default now()
);

comment on table limited_logins is
    '#6881: PostgreSQL logins limited to one agent and one host (deploy/sql/role-bureau.sql); written by the administrator only, read by ameesh_limited_login_guard.';

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
        -- écrits par les triggers des tables ci-dessus, jamais directement
        if pg_trigger_depth() < 2 then
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

-- `zz_` : après les triggers BEFORE existants (ordre alphabétique), donc sur
-- les valeurs normalisées (orthographe des agents, #5161).
drop trigger if exists zz_ameesh_limited_login on session_bindings;
create trigger zz_ameesh_limited_login before insert or update or delete on session_bindings
    for each row execute function ameesh_limited_login_guard();
drop trigger if exists zz_ameesh_limited_login on agent_mailbox;
create trigger zz_ameesh_limited_login before insert or update or delete on agent_mailbox
    for each row execute function ameesh_limited_login_guard();
drop trigger if exists zz_ameesh_limited_login on work_items;
create trigger zz_ameesh_limited_login before insert or update or delete on work_items
    for each row execute function ameesh_limited_login_guard();
drop trigger if exists zz_ameesh_limited_login on work_item_events;
create trigger zz_ameesh_limited_login before insert or update or delete on work_item_events
    for each row execute function ameesh_limited_login_guard();
drop trigger if exists zz_ameesh_limited_login on provenance_receipts;
create trigger zz_ameesh_limited_login before insert or update or delete on provenance_receipts
    for each row execute function ameesh_limited_login_guard();
drop trigger if exists zz_ameesh_limited_login on thread_index;
create trigger zz_ameesh_limited_login before insert or update or delete on thread_index
    for each row execute function ameesh_limited_login_guard();
drop trigger if exists zz_ameesh_limited_login on ameesh_events;
create trigger zz_ameesh_limited_login before insert or update or delete on ameesh_events
    for each row execute function ameesh_limited_login_guard();
drop trigger if exists zz_ameesh_limited_login on agent_registry;
create trigger zz_ameesh_limited_login before insert or update or delete on agent_registry
    for each row execute function ameesh_limited_login_guard();
drop trigger if exists zz_ameesh_limited_login on agent_name_resolutions;
create trigger zz_ameesh_limited_login before insert or update or delete on agent_name_resolutions
    for each row execute function ameesh_limited_login_guard();
