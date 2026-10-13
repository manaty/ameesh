-- SPDX-License-Identifier: AGPL-3.0-only
-- L244 (#420) : le rôle QA et son verdict dans la porte des releases IT.
--
-- Le module IT déclare une évidence `qa` (writer_role `ameesh_it_qa`,
-- actor_role `qa`) : le garde SQL vérifie que l'acteur tient le rôle `qa` pour
-- le projet du lot (registre lu sous verrou partagé) et qu'il diffère de
-- l'assigne, de l'auteur de la livraison et de celui de la preuve
-- (séparation des devoirs, refus `qa-same-actor`). Une règle `require_qa`
-- optionnelle exige un verdict `ok` lié à la livraison courante pour les états
-- qu'elle garde.

-- Les rôles déclarés d'une fiche Agent/Persona (`roles:`), comme `projects`
-- (0388) : `canon sync` les écrit, le garde SQL les lit.
alter table agent_registry add column if not exists roles text[];

-- La règle d'évidence accepte une valeur de verdict (`evidence_verdict`) : le
-- `qa` du module IT n'est satisfait que par un verdict `ok`, un verdict
-- `blocked` laisse la porte fermée.
create or replace function ameesh_module_requirements(item work_items, target_state text,
    milestone_kind text, milestone_verdict text) returns void language plpgsql as $$
declare
    project_name text := coalesce(nullif(item.app, ''), item.workstream, '');
    policy record;
    requirement jsonb;
    current_subject record;
    admitted boolean := item.type in ('bug', 'evolution', 'improvement');
begin
    if exists (select 1 from project_modules where project = project_name) then
        perform pg_advisory_xact_lock_shared(hashtext('ameesh-module-project'), hashtext(project_name));
    end if;
    for policy in select * from project_modules where project = project_name loop
        if policy.types ? item.type then admitted := true; end if;
        for requirement in select value from jsonb_array_elements(policy.rules) loop
            if not (requirement->'types' ? item.type) then continue; end if;
            if not (coalesce(requirement->'states' ? target_state, false)
                or coalesce(requirement->'milestones'->milestone_kind ? milestone_verdict, false))
                then continue; end if;
            select id, subject into current_subject from work_module_milestones
                where work_item_id = item.id and module = policy.module
                  and kind = requirement->>'subject_kind' order by id desc limit 1;
            if not found then
                raise exception 'AMEESH_MODULE_RULE: module %: lot % lacks current % milestone',
                    policy.module, item.id, requirement->>'subject_kind';
            end if;
            perform 1 from work_module_milestones e
                where e.work_item_id = item.id and e.module = policy.module
                  and e.kind = requirement->>'evidence_kind'
                  and e.subject = current_subject.subject and e.binding_id = current_subject.id
                  and (requirement->>'evidence_verdict' is null
                       or e.payload->>'verdict' = requirement->>'evidence_verdict');
            if not found then
                raise exception 'AMEESH_MODULE_RULE: module %: lot % lacks % for current subject %',
                    policy.module, item.id, requirement->>'evidence_kind', current_subject.subject;
            end if;
        end loop;
    end loop;
    if not admitted then
        raise exception 'AMEESH_MODULE_RULE: work item type % has no active module in project %',
            item.type, project_name;
    end if;
end $$;

-- Le garde d'évidence gagne deux contrôles déclarés par le schéma du module :
--   actor_role            : l'acteur tient ce rôle au registre, pour ce projet ;
--   separation_of_duties  : l'acteur diffère de l'assigne, de l'auteur de la
--                           déclaration liée et de l'auteur de l'évidence nommée.
create or replace function ameesh_module_milestone_guard() returns trigger language plpgsql as $$
declare
    item work_items;
    policy record;
    definition jsonb;
    field text;
    condition record;
    binding record;
    actor_row record;
    actor_projects text[];
    lot_project text;
    duties jsonb;
    other_actor text;
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
    if definition ? 'writer_role' and not ameesh_module_writer(definition->>'writer_role') then
        raise exception 'module evidence requires the isolated writer role';
    end if;
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
    if definition ? 'actor_role' then
        select roles, projects, team into actor_row
            from agent_registry where name = NEW.actor for share;
        if not found then
            raise exception 'AMEESH_MODULE_RULE: module %: %-role-required: actor % is not a registered agent',
                NEW.module, NEW.kind, NEW.actor;
        end if;
        if not (definition->>'actor_role' = any(coalesce(actor_row.roles, '{}'::text[]))) then
            raise exception 'AMEESH_MODULE_RULE: module %: %-role-required: actor % does not hold role %',
                NEW.module, NEW.kind, NEW.actor, definition->>'actor_role';
        end if;
        -- Un agent sans identité de projet est global ; sinon le lot doit être
        -- l'un de ses projets (le premier de la fiche étant le projet principal).
        actor_projects := coalesce(actor_row.projects,
            case when coalesce(actor_row.team, '') = '' then '{}'::text[]
                 else array[actor_row.team] end);
        lot_project := coalesce(nullif(item.app, ''), item.workstream, '');
        if cardinality(actor_projects) > 0 and not (lot_project = any(actor_projects)) then
            raise exception 'AMEESH_MODULE_RULE: module %: %-role-scope: actor % does not hold role % for project %',
                NEW.module, NEW.kind, NEW.actor, definition->>'actor_role', lot_project;
        end if;
    end if;
    if definition ? 'separation_of_duties' then
        duties := definition->'separation_of_duties';
        if coalesce((duties->>'assignee')::boolean, false) and NEW.actor = item.assignee then
            raise exception 'AMEESH_MODULE_RULE: module %: %-same-actor: the lot assignee cannot record this milestone',
                NEW.module, NEW.kind;
        end if;
        if coalesce((duties->>'declaration')::boolean, false) then
            select actor into other_actor from work_module_milestones where id = NEW.binding_id;
            if other_actor = NEW.actor then
                raise exception 'AMEESH_MODULE_RULE: module %: %-same-actor: the delivery author cannot record this milestone',
                    NEW.module, NEW.kind;
            end if;
        end if;
        if duties ? 'evidence' then
            -- Tout auteur d'une preuve liée à la livraison courante, pas
            -- seulement la dernière : une preuve réécrite par un autre ne
            -- rend pas l'auteur initial indépendant de sa propre exécution.
            perform 1 from work_module_milestones
                where work_item_id = NEW.work_item_id and module = NEW.module
                  and kind = duties->>'evidence' and subject = NEW.subject
                  and binding_id = NEW.binding_id and actor = NEW.actor;
            if found then
                raise exception 'AMEESH_MODULE_RULE: module %: %-same-actor: the evidence author cannot record this milestone',
                    NEW.module, NEW.kind;
            end if;
        end if;
    end if;
    return NEW;
end $$;
