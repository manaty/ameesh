-- SPDX-License-Identifier: AGPL-3.0-only
-- L122 / #711: one allocator, several named slots per lot and target.
create table if not exists work_migration_reservations (
    id bigserial primary key,
    work_item_id bigint not null references work_items(id) on delete cascade,
    slot text not null check (slot <> ''),
    number integer not null check (number between 1 and 9999),
    project text not null,
    target_repo text not null check (target_repo <> ''),
    target_branch text check (target_branch <> ''),
    target_sha text check (target_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    status text not null check (status in ('reserved', 'landed', 'released')),
    updated_at timestamptz not null default now(),
    unique (work_item_id, slot)
);
drop index if exists work_migration_reservations_active_idx;
create unique index work_migration_reservations_active_idx
    on work_migration_reservations(target_repo, target_branch, number)
    where status = 'reserved';

-- NULL target means the repository default is unknown to SQL. Such a
-- legacy record occupies its number on every branch until a target is set.
-- Serialize the cross-scope check, including clients predating this code.
create or replace function work_migration_reservation_guard() returns trigger
language plpgsql as $$
begin
    perform pg_advisory_xact_lock(hashtextextended(
        current_schema() || ':migration-repo:' || new.target_repo, 0));
    if new.status = 'reserved' and exists (
        select 1 from work_migration_reservations r
         where not (r.work_item_id = new.work_item_id and r.slot = new.slot)
           and r.status = 'reserved'
           and r.target_repo = new.target_repo and r.number = new.number
           and (r.target_branch = new.target_branch or r.target_branch is null
                or new.target_branch is null)
    ) then
        raise exception 'migration % of % already reserved on the target (or an unknown legacy target)',
            new.number, new.target_repo
            using errcode = '23505', constraint = 'work_migration_reservations_active_idx';
    end if;
    return new;
end $$;
drop trigger if exists work_migration_reservation_guard on work_migration_reservations;
create trigger work_migration_reservation_guard
    before insert or update on work_migration_reservations
    for each row execute function work_migration_reservation_guard();

-- Existing clients keep their single-slot projection. Legacy reservations
-- have no observed target SHA; they still occupy the same allocation space.
insert into work_migration_reservations
    (work_item_id, slot, number, project, target_repo, target_branch, status)
select id, 'default', migration, coalesce(app, ''), 'github.com/' || migration_repo,
       nullif(regexp_replace(branch_target, '^(refs/heads/|refs/remotes/origin/|origin/)', ''), ''),
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
                'github.com/' || new.migration_repo, nullif(regexp_replace(new.branch_target, '^(refs/heads/|refs/remotes/origin/|origin/)', ''), ''),
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
