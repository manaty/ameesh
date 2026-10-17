-- SPDX-License-Identifier: AGPL-3.0-only
-- L244 (#420) : le rôle QA et son verdict dans la porte des releases IT.
--
-- Le module IT déclare une évidence `qa` (writer_role `ameesh_it_qa`,
-- actor_role `qa`) : le garde SQL vérifie que l'acteur tient le rôle `qa` pour
-- le projet du lot (registre lu sous verrou partagé) et qu'il diffère de
-- l'assigne, de l'auteur de la livraison et de celui de la preuve
-- (séparation des devoirs, refus `qa-same-actor`). Une règle `require_qa`
-- optionnelle exige un verdict `ok` **dernier** lié à la livraison courante
-- pour les états qu'elle garde : un `blocked` ferme la porte jusqu'à un `ok`
-- ultérieur (R2 #420, B1).
--
-- R2 #420 (B2) : le garde lit la fiche de l'acteur par `ameesh_module_agent`,
-- une fonction SECURITY DEFINER à search_path figé. Un rôle isolé qui n'a que
-- SELECT (`ameesh_it_qa`) ne peut pas prendre lui-même le verrou `FOR SHARE`,
-- qui exige UPDATE ; cette fonction le fait pour lui, sans lui donner de
-- droits d'écriture sur le registre.
--
-- R2 #420 (B3) : la séparation des devoirs est revérifiée à la porte, et les
-- preuves d'un projet gardé par `require_qa` doivent être attribuables à un
-- agent lié à son exécuteur (`runner_bound_actor`) : l'assistant sudo écrivait
-- `uid:<SUDO_UID>`, jamais un nom d'agent, ce qui rendait le contrôle
-- inopérant en production.

-- Les rôles déclarés d'une fiche Agent/Persona (`roles:`), comme `projects`
-- (0388) : `canon sync` les écrit, le garde SQL les lit.
alter table agent_registry add column if not exists roles text[];

-- --------------------------------------------------------------------------
-- Lecture du registre pour les rôles isolés (R2 #420, B2)
-- --------------------------------------------------------------------------
-- Le garde lit la fiche sous verrou partagé. `FOR SHARE` exige UPDATE sur la
-- table : un rôle isolé qui n'a que SELECT ne peut pas le prendre. Cette
-- fonction, exécutée par le propriétaire du schéma avec le search_path figé
-- de la migration, fait la lecture pour lui. EXECUTE est retiré à PUBLIC :
-- seuls les rôles écrivains du module (et le propriétaire) l'appellent.
create or replace function ameesh_module_agent(p_name text)
returns table (roles text[], projects text[], team text,
               lease_owner text, lease_epoch bigint)
language plpgsql security definer set search_path from current as $$
begin
    return query
        select a.roles, a.projects, a.team, a.lease_owner, a.lease_epoch
          from agent_registry a
         where a.name = p_name
           for share;
end $$;

revoke all on function ameesh_module_agent(text) from public;
do $$
declare
    role_name text;
begin
    foreach role_name in array array['ameesh_it_qa', 'ameesh_it_proof'] loop
        if exists (select 1 from pg_roles where rolname = role_name) then
            execute format('grant execute on function ameesh_module_agent(text) to %I',
                           role_name);
        end if;
    end loop;
end $$;

-- La règle d'évidence accepte une valeur de verdict (`evidence_verdict`) : le
-- `qa` du module IT n'est satisfait que par un verdict `ok`, un verdict
-- `blocked` laisse la porte fermée.
create or replace function ameesh_module_requirements(item work_items, target_state text,
    milestone_kind text, milestone_verdict text) returns void language plpgsql as $$
declare
    project_name text := coalesce(nullif(item.app, ''), item.workstream, '');
    policy record;
    requirement jsonb;
    definition jsonb;
    current_subject record;
    evidence_verdict text;
    evidence_actor text;
    declaration_actor text;
    duties jsonb;
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
            select id, subject, actor into current_subject from work_module_milestones
                where work_item_id = item.id and module = policy.module
                  and kind = requirement->>'subject_kind' order by id desc limit 1;
            if not found then
                raise exception 'AMEESH_MODULE_RULE: module %: lot % lacks current % milestone',
                    policy.module, item.id, requirement->>'subject_kind';
            end if;
            -- Le DERNIER verdict de la livraison courante décide (R2 #420, B1) :
            -- un `ok` suivi d'un `blocked` referme la porte, l'audit
            -- `missing_qa` lit la même ligne.
            select e.payload->>'verdict', e.actor into evidence_verdict, evidence_actor
                from work_module_milestones e
                where e.work_item_id = item.id and e.module = policy.module
                  and e.kind = requirement->>'evidence_kind'
                  and e.subject = current_subject.subject
                  and e.binding_id = current_subject.id
                order by e.id desc limit 1;
            if not found then
                raise exception 'AMEESH_MODULE_RULE: module %: lot % lacks % for current subject %',
                    policy.module, item.id, requirement->>'evidence_kind', current_subject.subject;
            end if;
            if requirement->>'evidence_verdict' is not null
                and evidence_verdict is distinct from requirement->>'evidence_verdict' then
                raise exception 'AMEESH_MODULE_RULE: module %: lot % lacks % verdict % for current subject % (last verdict: %)',
                    policy.module, item.id, requirement->>'evidence_kind',
                    requirement->>'evidence_verdict', current_subject.subject,
                    coalesce(evidence_verdict, 'none');
            end if;
            -- R2 #420 (B3) : la séparation des devoirs est revérifiée ici, à
            -- chaque passage de porte, et pas seulement à l'écriture du
            -- verdict. Une preuve écrite après le verdict ne peut donc plus
            -- libérer son auteur.
            definition := policy.schemas->(requirement->>'evidence_kind');
            if definition ? 'separation_of_duties' then
                duties := definition->'separation_of_duties';
                if coalesce((duties->>'assignee')::boolean, false)
                    and evidence_actor = item.assignee then
                    raise exception 'AMEESH_MODULE_RULE: module %: %-same-actor: the lot assignee cannot record this milestone',
                        policy.module, requirement->>'evidence_kind';
                end if;
                if coalesce((duties->>'declaration')::boolean, false) then
                    declaration_actor := current_subject.actor;
                    if declaration_actor = evidence_actor then
                        raise exception 'AMEESH_MODULE_RULE: module %: %-same-actor: the delivery author cannot record this milestone',
                            policy.module, requirement->>'evidence_kind';
                    end if;
                end if;
                if duties ? 'evidence' then
                    perform 1 from work_module_milestones p
                        where p.work_item_id = item.id and p.module = policy.module
                          and p.kind = duties->>'evidence'
                          and p.subject = current_subject.subject
                          and p.binding_id = current_subject.id
                          and p.actor = evidence_actor;
                    if found then
                        raise exception 'AMEESH_MODULE_RULE: module %: %-same-actor: the evidence author cannot record this milestone',
                            policy.module, requirement->>'evidence_kind';
                    end if;
                end if;
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
--                           déclaration liée et de l'auteur de l'évidence nommée;
--   runner_bound_actor    : l'acteur est un agent lié à son exécuteur (le
--                           payload porte `agent`, `runner_id`, `lease_epoch`).
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
        select a.roles, a.projects, a.team into actor_row
            from ameesh_module_agent(NEW.actor) a;
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
    -- R2 #420 (B3) : une preuve attribuable. L'assistant sudo écrivait
    -- `uid:<SUDO_UID>` ; un projet gardé par `require_qa` déclare
    -- `runner_bound_actor` et exige l'agent lié à son exécuteur.
    if coalesce((definition->>'runner_bound_actor')::boolean, false) then
        if jsonb_typeof(NEW.payload->'agent') is distinct from 'string'
            or NEW.payload->>'agent' is distinct from NEW.actor
            or jsonb_typeof(NEW.payload->'runner_id') is distinct from 'string'
            or btrim(NEW.payload->>'runner_id') = ''
            or jsonb_typeof(NEW.payload->'lease_epoch') is distinct from 'number' then
            raise exception 'AMEESH_MODULE_RULE: module %: %-identity-required: the actor must name the runner-bound agent',
                NEW.module, NEW.kind;
        end if;
        select a.lease_owner, a.lease_epoch into actor_row
            from ameesh_module_agent(NEW.actor) a;
        if not found
            or actor_row.lease_owner is distinct from NEW.payload->>'runner_id'
            or actor_row.lease_epoch is distinct from (NEW.payload->>'lease_epoch')::bigint then
            raise exception 'AMEESH_MODULE_RULE: module %: %-identity-unbound: actor % is not bound to runner %',
                NEW.module, NEW.kind, NEW.actor, NEW.payload->>'runner_id';
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
    -- R2 #420 (B3) : symétrie. Un acteur qui a déjà signé le verdict QA de
    -- cette livraison ne peut pas écrire ensuite la preuve nommée par la
    -- séparation des devoirs (et réciproquement le cas est couvert plus haut).
    if NEW.binding_id is not null then
        for condition in select key as kind from jsonb_each(policy.schemas)
            where value->'separation_of_duties'->>'evidence' = NEW.kind loop
            perform 1 from work_module_milestones
                where work_item_id = NEW.work_item_id and module = NEW.module
                  and kind = condition.kind and binding_id = NEW.binding_id
                  and subject = NEW.subject and actor = NEW.actor;
            if found then
                raise exception 'AMEESH_MODULE_RULE: module %: %-same-actor: the QA verdict author cannot record this evidence',
                    NEW.module, condition.kind;
            end if;
        end loop;
    end if;
    return NEW;
end $$;
