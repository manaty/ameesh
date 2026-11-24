-- SPDX-License-Identifier: AGPL-3.0-only
-- #8191: admit personal follow-ups; preserve the current QA evidence guards (0427).
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
    admitted boolean := item.type in ('bug', 'evolution', 'improvement', 'follow-up');
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

-- ameesh: constraint not tightened
-- Every pre-existing type satisfies the first disjunct. Only the newly admitted
-- follow-up type is constrained; previous code could not create such a row.
alter table work_items add constraint work_items_follow_up_personal check (
    type <> 'follow-up' or (
        nullif(btrim(assignee), '') is not null
        and branch is null and branch_target is null and pr_ref is null
        and estimate_minutes is null and change_size is null
        and state in ('intake', 'blocked', 'closed')
    )
);
