-- SPDX-License-Identifier: AGPL-3.0-only
-- #5087: reversible review hold; existing evidence is never invalidated.
alter table agent_registry add column review_hold jsonb;

create table work_review_withdrawals (
    work_item_id bigint not null references work_items(id) on delete cascade,
    reviewer text not null,
    withdrawn boolean not null default true,
    primary key (work_item_id, reviewer)
);

create trigger ameesh_agent_spelling before insert or update of reviewer
on work_review_withdrawals for each row
execute function ameesh_agent_spelling('reviewer');

-- Read the same agent row the hold writer updates, so selection/verdict and
-- hold have a transaction order. No SECURITY DEFINER, no authority expansion.
create function ameesh_review_hold_check(spelling text) returns void
language plpgsql security invoker set search_path from current as $$
declare held jsonb;
begin
    select review_hold into held from agent_registry
     where name = spelling or spelling = any(aliases) for share;
    if held is not null then
        raise exception 'review refused: % has a review hold: %', spelling, held->>'reason';
    end if;
end $$;

create function ameesh_review_seats_guard() returns trigger
language plpgsql security invoker set search_path from current as $$
declare spelling text;
begin
    for spelling in
        select coalesce(ameesh_agent_primary_locked(n), n)
          from unnest(coalesce(new.reviewers, '{}'::text[])) n
        except
        select coalesce(ameesh_agent_primary_locked(n), n)
          from unnest(coalesce(old.reviewers, '{}'::text[])) n
    loop
        perform ameesh_review_hold_check(spelling);
        update work_review_withdrawals set withdrawn = false
         where work_item_id = new.id and reviewer = spelling;
    end loop;
    -- Automatic seat rotation is not an explicit withdrawal. The caller
    -- scopes this transaction setting to its single reviewers UPDATE.
    if current_setting('ameesh.review_seat_rotation', true) = 'on' then
        return new;
    end if;
    for spelling in
        select coalesce(ameesh_agent_primary_locked(n), n)
          from unnest(coalesce(old.reviewers, '{}'::text[])) n
        except
        select coalesce(ameesh_agent_primary_locked(n), n)
          from unnest(coalesce(new.reviewers, '{}'::text[])) n
    loop
        insert into work_review_withdrawals(work_item_id, reviewer)
        values (new.id, spelling)
        on conflict (work_item_id, reviewer) do update set withdrawn = true;
    end loop;
    return new;
end $$;
-- AFTER sees the reviewers array normalized by the existing 0456 trigger.
create trigger ameesh_review_seats_guard after insert or update of reviewers, reviewer
on work_items for each row execute function ameesh_review_seats_guard();

create function ameesh_review_verdict_guard() returns trigger
language plpgsql security invoker set search_path from current as $$
begin
    if new.kind = 'verdict' then
        -- Removal and verdict serialize on the lot, including direct writers.
        perform id from work_items where id = new.work_item_id for no key update;
        perform ameesh_review_hold_check(new.actor);
        if exists (select 1 from work_review_withdrawals w
                    where w.withdrawn and w.work_item_id = new.work_item_id
                      and w.reviewer = coalesce(ameesh_agent_primary_locked(new.actor), new.actor)) then
            raise exception 'verdict refused: % was withdrawn from lot %; designate again first',
                new.actor, new.work_item_id;
        end if;
    end if;
    return new;
end $$;
create trigger ameesh_review_verdict_guard before insert on work_item_milestones
for each row execute function ameesh_review_verdict_guard();
