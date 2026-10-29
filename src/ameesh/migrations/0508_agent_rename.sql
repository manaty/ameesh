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
