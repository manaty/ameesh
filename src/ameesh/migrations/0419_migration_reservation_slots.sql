-- SPDX-License-Identifier: AGPL-3.0-only
-- L122 / #711: one allocator, several named slots per lot and target.
create table if not exists work_migration_reservations (
    id bigserial primary key,
    work_item_id bigint not null references work_items(id) on delete cascade,
    slot text not null check (slot <> ''),
    number integer not null check (number between 1 and 9999),
    project text not null,
    target_repo text not null check (target_repo <> ''),
    target_branch text not null check (target_branch <> ''),
    target_sha text check (target_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    status text not null check (status in ('reserved', 'landed', 'released')),
    updated_at timestamptz not null default now(),
    unique (work_item_id, slot)
);
create unique index if not exists work_migration_reservations_active_idx
    on work_migration_reservations(project, target_repo, target_branch, number)
    where status = 'reserved';

-- Existing clients keep their single-slot projection. Legacy reservations
-- have no observed target SHA; they still occupy the same allocation space.
insert into work_migration_reservations
    (work_item_id, slot, number, project, target_repo, target_branch, status)
select id, 'default', migration, coalesce(app, ''), 'github.com/' || migration_repo,
       regexp_replace(coalesce(nullif(branch_target, ''), 'main'), '^(refs/heads/|refs/remotes/origin/|origin/)', ''),
       case when state in ('merged', 'promoted') then 'landed'
            when state = 'closed' then 'released' else 'reserved' end
from work_items where migration is not null
on conflict (work_item_id, slot) do nothing;

create or replace function work_items_migration_slot_sync() returns trigger
language plpgsql as $$
begin
    if new.migration is null then
        update work_migration_reservations set status = 'released', updated_at = now()
        where work_item_id = new.id and slot = 'default' and status = 'reserved';
    else
        insert into work_migration_reservations
            (work_item_id, slot, number, project, target_repo, target_branch, status)
        values (new.id, 'default', new.migration, coalesce(new.app, ''),
                'github.com/' || new.migration_repo, regexp_replace(coalesce(nullif(new.branch_target, ''), 'main'), '^(refs/heads/|refs/remotes/origin/|origin/)', ''),
                case when new.state in ('merged', 'promoted') then 'landed'
                     when new.state = 'closed' then 'released' else 'reserved' end)
        on conflict (work_item_id, slot) do update
            set number = excluded.number, project = excluded.project,
                target_repo = excluded.target_repo, target_branch = excluded.target_branch,
                status = excluded.status, updated_at = now();
    end if;
    return new;
end $$;
drop trigger if exists work_items_migration_slot_sync on work_items;
create trigger work_items_migration_slot_sync
    after insert or update of migration, migration_repo, app, branch_target, state on work_items
    for each row execute function work_items_migration_slot_sync();
