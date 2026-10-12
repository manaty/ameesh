-- SPDX-License-Identifier: AGPL-3.0-only
-- Generic module declarations and structured milestones (L317a, #318).
-- Domain-specific names and requirements come exclusively from modules.
create table project_modules (
    project text not null,
    module text not null,
    canon text not null,
    types jsonb not null check (jsonb_typeof(types) = 'array'),
    schemas jsonb not null check (jsonb_typeof(schemas) = 'object'),
    rules jsonb not null check (jsonb_typeof(rules) = 'array'),
    primary key (project, module)
);

create table work_module_milestones (
    id bigserial primary key,
    work_item_id bigint not null references work_items(id),
    module text not null,
    kind text not null,
    subject text not null check (length(subject) between 1 and 256),
    binding_id bigint references work_module_milestones(id),
    payload jsonb not null check (jsonb_typeof(payload) = 'object'
                                 and octet_length(payload::text) <= 65536),
    actor text not null,
    at timestamptz not null default now()
);
create index work_module_milestones_item_idx
    on work_module_milestones (work_item_id, module, kind, id desc);

create function ameesh_module_policy_lock() returns trigger language plpgsql as $$
begin
    perform pg_advisory_xact_lock(hashtext('ameesh-module-project'),
        hashtext(case when TG_OP = 'DELETE' then OLD.project else NEW.project end));
    if TG_OP = 'UPDATE' and (OLD.project, OLD.module, OLD.canon)
        is distinct from (NEW.project, NEW.module, NEW.canon) then
        raise exception 'module policy identity is immutable';
    end if;
    if TG_OP = 'DELETE' then return OLD; end if;
    return NEW;
end $$;
create trigger ameesh_module_policy_lock before insert or update or delete
    on project_modules for each row execute function ameesh_module_policy_lock();

-- The existing type check is extended by a guarded installed-module lookup.
-- Its existing three values retain exactly their old admission behavior.
alter table work_items drop constraint work_items_type_check;
create function ameesh_module_requirements(item work_items, target_state text,
    milestone_kind text, milestone_verdict text) returns void language plpgsql as $$
declare
    project_name text := coalesce(nullif(item.app, ''), item.workstream, '');
    policy record;
    requirement jsonb;
    current_subject record;
    admitted boolean := item.type in ('bug', 'evolution', 'improvement');
begin
    perform pg_advisory_xact_lock(hashtext('ameesh-module-project'), hashtext(project_name));
    for policy in select * from project_modules where project = project_name loop
        if policy.types ? item.type then admitted := true; end if;
        for requirement in select value from jsonb_array_elements(policy.rules) loop
            if not (requirement->'types' ? item.type) then continue; end if;
            if not (coalesce(requirement->'states' ? target_state, false)
                or coalesce(requirement->'milestones'->milestone_kind ? milestone_verdict, false))
                then continue;
            end if;
            select id, subject into current_subject from work_module_milestones
                where work_item_id = item.id and module = policy.module
                  and kind = requirement->>'subject_kind' order by id desc limit 1;
            if not found then
                raise exception 'module %: lot % lacks current % milestone',
                    policy.module, item.id, requirement->>'subject_kind';
            end if;
            perform 1 from work_module_milestones
                where work_item_id = item.id and module = policy.module
                  and kind = requirement->>'evidence_kind'
                  and subject = current_subject.subject and binding_id = current_subject.id;
            if not found then
                raise exception 'module %: lot % lacks % for current subject %',
                    policy.module, item.id, requirement->>'evidence_kind', current_subject.subject;
            end if;
        end loop;
    end loop;
    if not admitted then
        raise exception 'work item type % has no active module in project %', item.type, project_name;
    end if;
end $$;

create function ameesh_module_work_guard() returns trigger language plpgsql as $$
begin
    if TG_OP = 'UPDATE' and OLD.type not in ('bug','evolution','improvement')
        and (OLD.type, OLD.app, OLD.workstream)
            is distinct from (NEW.type, NEW.app, NEW.workstream) then
        raise exception 'module work item identity is immutable';
    end if;
    -- Every write validates type admission; a closed item is checked again
    -- on terminal state entry, not on an unrelated journal/metadata update.
    if TG_OP = 'INSERT' or OLD.state is distinct from NEW.state
        or (OLD.type, OLD.app, OLD.workstream) is distinct from (NEW.type, NEW.app, NEW.workstream) then
        perform ameesh_module_requirements(NEW, NEW.state, null, null);
    else
        perform ameesh_module_requirements(NEW, null, null, null);
    end if;
    return NEW;
end $$;
create trigger ameesh_module_work_guard before insert or update on work_items
    for each row execute function ameesh_module_work_guard();

create function ameesh_module_verdict_guard() returns trigger language plpgsql as $$
declare item work_items;
begin
    select * into item from work_items where id = NEW.work_item_id for no key update;
    perform ameesh_module_requirements(item, null, NEW.kind, NEW.verdict);
    return NEW;
end $$;
create trigger ameesh_module_verdict_guard before insert or update on work_item_milestones
    for each row execute function ameesh_module_verdict_guard();

create function ameesh_module_milestone_guard() returns trigger language plpgsql as $$
declare
    item work_items;
    policy record;
    definition jsonb;
    field text;
    condition record;
    binding record;
begin
    if TG_OP <> 'INSERT' then raise exception 'module milestones are append-only'; end if;
    select * into item from work_items where id = NEW.work_item_id for no key update;
    if not found then raise exception 'work item does not exist'; end if;
    perform ameesh_module_requirements(item, null, null, null);
    select * into policy from project_modules
        where project = coalesce(nullif(item.app,''), item.workstream, '') and module = NEW.module;
    if not found or not (policy.types ? item.type) then
        raise exception 'module % is not active for this work item type', NEW.module;
    end if;
    definition := policy.schemas->NEW.kind;
    if definition is null then raise exception 'unknown module milestone %', NEW.kind; end if;
    for field in select jsonb_array_elements_text(coalesce(definition->'strings','[]')) loop
        if jsonb_typeof(NEW.payload->field) is distinct from 'string'
            or btrim(NEW.payload->>field) = '' then
            raise exception 'module milestone field % must be a nonempty string', field;
        end if;
    end loop;
    for field in select jsonb_array_elements_text(coalesce(definition->'arrays','[]')) loop
        if jsonb_typeof(NEW.payload->field) is distinct from 'array'
            or NEW.payload->field = '[]'::jsonb then
            raise exception 'module milestone field % must be a nonempty array', field;
        end if;
    end loop;
    for condition in select * from jsonb_each(coalesce(definition->'equals','{}')) loop
        if NEW.payload->condition.key is distinct from condition.value then
            raise exception 'module milestone field % violates its schema', condition.key;
        end if;
    end loop;
    for condition in select * from jsonb_each_text(coalesce(definition->'patterns','{}')) loop
        if (NEW.payload->>condition.key) is null
            or not ((NEW.payload->>condition.key) ~ condition.value) then
            raise exception 'module milestone field % violates its pattern', condition.key;
        end if;
    end loop;
    if definition ? 'subject_field' and
        (NEW.payload->>(definition->>'subject_field')) is distinct from NEW.subject then
        raise exception 'module milestone subject does not match its payload';
    end if;
    if definition ? 'binding_kind' then
        select id, subject into binding from work_module_milestones
            where work_item_id = NEW.work_item_id and module = NEW.module
              and kind = definition->>'binding_kind' order by id desc limit 1;
        if not found or NEW.binding_id is distinct from binding.id
            or NEW.subject is distinct from binding.subject then
            raise exception 'module milestone does not match the current subject declaration';
        end if;
    elsif NEW.binding_id is not null then
        raise exception 'this module milestone has no binding';
    end if;
    return NEW;
end $$;
create trigger ameesh_module_milestone_guard before insert or update or delete
    on work_module_milestones for each row execute function ameesh_module_milestone_guard();
