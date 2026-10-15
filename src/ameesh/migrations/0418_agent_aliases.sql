-- SPDX-License-Identifier: AGPL-3.0-only
-- L243b (#423), step 1 only: alternate names of a single registry identity.
-- No existing agent is renamed, no state or key is copied.
alter table agent_registry add column if not exists aliases text[];
create index if not exists agent_registry_aliases on agent_registry using gin (aliases);

create table if not exists agent_name_resolutions (
    id bigserial primary key,
    at timestamptz not null default clock_timestamp(),
    supplied_name text not null,
    primary_name text not null,
    operation text not null,
    db_user text not null default current_user
);

-- Namespace writers serialize before checking both registries. At stronger
-- isolation a stale snapshot must never make a second identity admissible.
create or replace function ameesh_agent_names_guard() returns trigger
language plpgsql as $$
declare
    spellings text[];
    spelling text;
begin
    if current_setting('transaction_isolation') <> 'read committed' then
        raise exception 'agent names and aliases require read committed'
            using errcode = 'serialization_failure';
    end if;
    if tg_table_name = 'key_holders' then
        perform key_name_lock(new.name, tg_table_schema);
        if exists (select 1 from agent_registry
                    where new.name = any(aliases)) then
            raise exception 'human key holder name % is an agent name or alias', new.name
                using errcode = 'unique_violation';
        end if;
        return new;
    end if;
    spellings := array[new.name] || coalesce(new.aliases, '{}'::text[]);
    for spelling in select distinct n from unnest(spellings) n order by n loop
        perform key_name_lock(spelling, tg_table_schema);
    end loop;
    if cardinality(spellings) <> (select count(distinct n) from unnest(spellings) n) then
        raise exception 'duplicate agent name or alias for %', new.name
            using errcode = 'check_violation';
    end if;
    foreach spelling in array spellings loop
        if spelling is null or length(spelling) > 64
           or spelling !~ '^[A-Za-z0-9][A-Za-z0-9._-]*$'
           or (spelling = 'all' and spelling <> new.name) then
            raise exception 'invalid agent name or alias: %', spelling
                using errcode = 'check_violation';
        end if;
        if exists (select 1 from agent_registry r
                    where r.name <> case when tg_op = 'UPDATE' then old.name else new.name end
                      and (spelling = any(r.aliases)
                           or (spelling <> new.name and r.name = spelling)))
           or (spelling <> new.name and exists (select 1 from key_holders where name = spelling)) then
            raise exception 'agent name or alias % already belongs to another identity', spelling
                using errcode = 'unique_violation';
        end if;
    end loop;
    return new;
end
$$;

create trigger ameesh_agent_names_guard
    before insert or update of name, aliases on agent_registry
    for each row execute function ameesh_agent_names_guard();
create trigger ameesh_human_aliases_guard
    before insert or update of name on key_holders
    for each row execute function ameesh_agent_names_guard();

-- Volatile: audit the spelling before an authority check can consume it.
-- A failed/rolled-back transaction leaves no successful resolution event.
create or replace function ameesh_resolve_agent_name(supplied text, context text)
returns text language plpgsql volatile as $$
declare primary_name text;
begin
    select r.name into primary_name from agent_registry r
     where r.name = supplied or supplied = any(r.aliases);
    if primary_name is null then return supplied; end if;
    if primary_name <> supplied then
        insert into agent_name_resolutions (supplied_name, primary_name, operation)
            values (supplied, primary_name, context);
    end if;
    return primary_name;
end
$$;

-- Resolve/guard against these tables regardless of the caller's search path;
-- pg_temp is explicitly last, so a temporary shadow cannot replace them.
do $$ begin
    execute format('alter function ameesh_agent_names_guard() set search_path to pg_catalog, %I, pg_temp', current_schema());
    execute format('alter function ameesh_resolve_agent_name(text, text) set search_path to pg_catalog, %I, pg_temp', current_schema());
end $$;

create or replace function ameesh_agent_name_audit_guard() returns trigger
language plpgsql as $$ begin
    raise exception 'agent_name_resolutions is append-only'
        using errcode = 'insufficient_privilege';
end $$;
create trigger ameesh_agent_name_audit_guard before update or delete on agent_name_resolutions
    for each row execute function ameesh_agent_name_audit_guard();
