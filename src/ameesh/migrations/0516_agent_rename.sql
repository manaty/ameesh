-- SPDX-License-Identifier: AGPL-3.0-only
-- L243b step 3 (#4299, decision #157436 a): rename an agent identity in place.
-- The registry row keeps its key, lease epoch and session; its children follow
-- the primary through ON UPDATE CASCADE instead of a second row or a copy;
-- so do the conversation log's rows, children of conversation_heads(agent).
do $$
declare
    fk record;
begin
    for fk in select c.conname, c.conrelid::regclass as child, pg_get_constraintdef(c.oid) as def
                from pg_constraint c
               where c.contype = 'f'
                 and c.confrelid in ('agent_registry'::regclass, 'conversation_heads'::regclass)
                 and c.confupdtype <> 'c' loop
        execute format('alter table %s drop constraint %I', fk.child, fk.conname);
        execute format('alter table %s add constraint %I %s on update cascade',
                       fk.child, fk.conname, fk.def);
    end loop;
end $$;

-- One row per committed rename, written in the rename's own transaction:
-- a rolled-back attempt leaves nothing. Reverse renames are new rows.
create table if not exists agent_renames (
    id bigserial primary key,
    at timestamptz not null default clock_timestamp(),
    old_name text not null,
    new_name text not null,
    actor text not null,
    reason text not null,
    lease_epoch bigint,
    rewritten jsonb not null,
    db_user text not null default current_user
);

create or replace function ameesh_agent_renames_guard() returns trigger
language plpgsql as $$ begin
    raise exception 'agent_renames is append-only'
        using errcode = 'insufficient_privilege';
end $$;
drop trigger if exists ameesh_agent_renames_guard on agent_renames;
create trigger ameesh_agent_renames_guard before update or delete on agent_renames
    for each row execute function ameesh_agent_renames_guard();

-- One column of the rename: rows where the column equals (or, for an array,
-- contains) the old spelling. The caller names the column from its fixed list.
create or replace function ameesh_rename_agent_column(tbl text, col text, is_array boolean,
                                                      old_name text, new_name text)
returns bigint language plpgsql as $$
declare moved bigint;
begin
    if is_array then
        execute format('update %I set %I = array_replace(%I, $1, $2) where $1 = any(%I)',
                       tbl, col, col, col) using old_name, new_name;
    else
        execute format('update %I set %I = $2 where %I = $1', tbl, col, col)
            using old_name, new_name;
    end if;
    get diagnostics moved = row_count;
    return moved;
end $$;

-- B1 of the #4299 review: a writer that resolved OLD before the rename commit
-- must not insert OLD after it. Every rewritten column maps an alias to its
-- primary at write time, under FOR KEY SHARE of the registry row: the rename
-- holds FOR UPDATE on it, so a concurrent writer either waits for the commit
-- and then reads the new primary (rechecked on the updated row), or holds the
-- row first and the rename, waiting for it, rewrites what it wrote.
-- FOR KEY SHARE requires UPDATE on the registry, which every writer of these
-- tables holds (schema owner, ameesh_executor, ameesh_canon_sync): the lookup
-- runs with the caller's rights. SECURITY DEFINER would hand any role the
-- registry lock, and the role contracts refuse an executable one (h-12, #4477).
create or replace function ameesh_agent_primary_locked(spelling text)
returns text language plpgsql security invoker set search_path from current as $$
declare primary_name text;
begin
    select r.name into primary_name from agent_registry r
     where r.name = spelling or spelling = any(r.aliases)
       for key share;
    return primary_name;
end $$;

create or replace function ameesh_agent_spelling() returns trigger
language plpgsql set search_path from current as $$
declare
    spec text;
    col text;
    rec jsonb := to_jsonb(new);
    before jsonb := case when tg_op = 'UPDATE' then to_jsonb(old) else '{}'::jsonb end;
    patch jsonb := '{}'::jsonb;
    spelling text;
    primary_name text;
    names text[];
begin
    foreach spec in array string_to_array(tg_argv[0], ' ') loop
        col := replace(spec, '[]', '');
        -- an unchanged value is not a new write; null holds no spelling
        if coalesce(jsonb_typeof(rec -> col), 'null') not in ('string', 'array')
           or (rec -> col) is not distinct from (before -> col) then
            continue;
        end if;
        if right(spec, 2) = '[]' then
            select array_agg(coalesce(ameesh_agent_primary_locked(e), e) order by o)
              into names from jsonb_array_elements_text(rec -> col) with ordinality t(e, o);
            names := coalesce(names, '{}'::text[]);
            if to_jsonb(names) is distinct from rec -> col then
                patch := patch || jsonb_build_object(col, to_jsonb(names));
            end if;
        else
            spelling := rec ->> col;
            primary_name := ameesh_agent_primary_locked(spelling);
            if primary_name is not null and primary_name <> spelling then
                patch := patch || jsonb_build_object(col, primary_name);
            end if;
        end if;
    end loop;
    if patch <> '{}'::jsonb then
        new := jsonb_populate_record(new, patch);
    end if;
    return new;
end $$;

-- Same list as agent_rename.REWRITTEN (a test compares them).
do $$
declare
    spec text[];
begin
    foreach spec slice 1 in array array[
        ['account_budgets', 'set_by'],
        ['account_rate_events', 'agent'],
        ['account_reservations', 'agent closed_by'],
        ['account_switches', 'agent'],
        ['account_turn_slots', 'agent'],
        ['account_turn_starts', 'agent'],
        ['agent_lot_sessions', 'agent'],
        ['agent_mailbox', 'recipient sender'],
        ['agent_registry', 'created_by operator_pause_by retired_by'],
        ['agent_watches', 'agent enabled_by'],
        ['alert_acknowledgements', 'actor'],
        ['budget_events', 'actor'],
        ['budget_limits', 'set_by'],
        ['commitments', 'created_by owner'],
        ['conversation_heads', 'agent'],
        ['estimate_references', 'published_by'],
        ['host_lifecycle', 'actor return_requested_by'],
        ['host_lifecycle_transitions', 'actor'],
        ['housekeeping_log', 'actor agent'],
        ['integration_train_fires', 'changed_by owner'],
        ['managed_worktrees', 'agent'],
        ['merge_locks', 'owner'],
        ['merge_queue', 'queued_by'],
        ['notify_briefs', 'recipient'],
        ['notify_state', 'owner'],
        ['operator_pause_events', 'actor'],
        ['persona_sessions', 'persona'],
        ['provider_account_credits', 'agent prepaid_by'],
        ['review_catch_up', 'actor'],
        ['session_bindings', 'agent created_by'],
        ['spend_pending', 'agent'],
        ['standing_reservations', 'reserved_by'],
        ['turn_costs', 'agent'],
        ['turn_resources', 'agent'],
        ['visibility_checks', 'persona'],
        ['work_item_delegations', 'delegate delegated_by resolved_by'],
        ['work_item_dependencies', 'created_by'],
        ['work_item_estimates', 'estimated_by'],
        ['work_item_events', 'actor'],
        ['work_item_footprints', 'created_by'],
        ['work_item_links', 'created_by decided_by'],
        ['work_item_milestones', 'actor'],
        ['work_items', 'assignee delegated_by estimate_by external_executor planned_by reviewer reviewers[]'],
        ['work_landings', 'created_by'],
        ['work_packages', 'planned_by'],
        ['work_packet_members', 'recorded_by'],
        ['work_resolutions', 'created_by']] loop
        execute format('drop trigger if exists ameesh_agent_spelling on %I', spec[1]);
        execute format('create trigger ameesh_agent_spelling before insert or update of %s on %I'
                       ' for each row execute function ameesh_agent_spelling(%L)',
                       (select string_agg(quote_ident(replace(c, '[]', '')), ', ')
                          from unnest(string_to_array(spec[2], ' ')) c),
                       spec[1], spec[2]);
    end loop;
end $$;
