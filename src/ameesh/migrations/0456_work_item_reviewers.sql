-- SPDX-License-Identifier: AGPL-3.0-only
-- #2076: designated reviewers accumulate; reviewer remains the last one for
-- older runners. The trigger also handles their assignments and delegations.
alter table work_items add column if not exists reviewers text[] not null default '{}';

create or replace function work_item_reviewers_sync() returns trigger
language plpgsql as $$
declare
    names text[];
    canonical text[] := '{}';
    name text;
begin
    names := coalesce(new.reviewers, '{}');
    if tg_op = 'INSERT' then
        names := array_append(names, new.reviewer);
    elsif new.reviewers is not distinct from old.reviewers then
        -- Legacy writers only know reviewer. A designation adds a name;
        -- clearing it withdraws that name, preserving the other reviewers.
        if new.reviewer is distinct from old.reviewer then
            if coalesce(new.reviewer, '') = '' then
                names := array_remove(names, old.reviewer);
            else
                names := array_append(names, new.reviewer);
            end if;
        end if;
    end if;
    foreach name in array names loop
        if coalesce(name, '') <> '' and name is distinct from new.assignee
           and not name = any(canonical) then
            canonical := array_append(canonical, name);
        end if;
    end loop;
    new.reviewers := canonical;
    new.reviewer := coalesce(canonical[cardinality(canonical)], '');
    return new;
end;
$$;

drop trigger if exists work_item_reviewers_sync on work_items;
create trigger work_item_reviewers_sync
before insert or update of reviewer, reviewers, assignee on work_items
for each row execute function work_item_reviewers_sync();

-- Existing single designations survive the additive migration.
update work_items set reviewers = array[reviewer]
where cardinality(reviewers) = 0 and reviewer <> '';
