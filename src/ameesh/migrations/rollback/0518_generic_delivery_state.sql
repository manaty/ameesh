-- SPDX-License-Identifier: AGPL-3.0-only
-- L99 T5 (#4353): operator rollback, executors stopped, one transaction.
-- This is deliberately outside migration discovery. Never run automatically.
DROP TRIGGER IF EXISTS aa_work_items_generic_state_sync ON work_items;
ALTER TABLE work_items DROP CONSTRAINT IF EXISTS work_items_state_check;
ALTER TABLE work_items DROP CONSTRAINT IF EXISTS work_items_legacy_state_matches;
ALTER TABLE work_items DROP CONSTRAINT IF EXISTS work_items_workflow_stage_needs_ref;
UPDATE work_items SET state = legacy_state,
 workflow_stage = CASE WHEN coalesce(workflow_ref, '') = '' THEN NULL ELSE workflow_stage END;
ALTER TABLE work_items ADD CONSTRAINT work_items_state_check
 CHECK (state IN ('intake', 'build', 'qa', 'validated', 'merged', 'promoted',
                  'blocked', 'waiting_human', 'closed'));
ALTER TABLE work_items ADD CONSTRAINT work_items_workflow_stage_needs_ref
 CHECK (workflow_stage IS NULL OR workflow_ref IS NOT NULL);

create or replace function agent_mesh_work_item_milestones() returns trigger
language plpgsql as $$
begin
    if tg_op = 'INSERT' then
        execute format(
            'insert into %I.work_item_milestones (work_item_id, kind, at, note)'
            ' values ($1, ''requested'', $2, ''création'') on conflict do nothing',
            tg_table_schema) using new.id, new.created_at;
    elsif new.state = 'merged' and old.state is distinct from 'merged' then
        -- Un jalon par fusion EFFECTIVE : un retour en merged depuis
        -- promoted, blocked ou waiting_human n'en écrit pas un second, sauf
        -- si une correction `merged → build` a annulé le précédent (le
        -- prédicat lit la transition du journal, pas le texte de la note).
        execute format(
            'insert into %1$I.work_item_milestones (work_item_id, kind, at, note)'
            ' select $1, ''merged'', clock_timestamp(), ''transition'''
            '  where not exists (select 1 from %1$I.work_item_milestones m'
            '   where m.work_item_id = $1 and m.kind = ''merged'''
            '     and not %1$I.agent_mesh_merged_annulled($1, m.at))',
            tg_table_schema) using new.id;
    end if;
    return new;
end $$;

create or replace function work_items_migration_slot_sync() returns trigger
language plpgsql as $$
declare
    slot_status text;
begin
    if new.migration is null then
        update work_migration_reservations
           set status = case when new.state in ('merged', 'promoted') then 'landed'
                             else 'released' end,
               updated_at = now()
         where work_item_id = new.id and slot = 'default' and status = 'reserved';
    else
        slot_status := case when new.state in ('merged', 'promoted') then 'landed'
                            when new.state = 'closed' then 'released' else 'reserved' end;
        -- #879 : la ligne existante est mise a jour, jamais reinseree ; un
        -- `landed` de meme numero le reste.
        -- #879 N (#2658) : y compris quand la projection le donnerait
        -- `released` (`merged -> closed`), pas seulement `reserved`.
        update work_migration_reservations
           set number = new.migration, project = coalesce(new.app, ''),
               target_repo = 'github.com/' || new.migration_repo,
               target_branch = nullif(regexp_replace(new.branch_target, '^(refs/heads/|refs/remotes/origin/|origin/)', ''), ''),
               status = case when work_migration_reservations.status = 'landed'
                              and slot_status in ('reserved', 'released')
                              and work_migration_reservations.number = new.migration
                             then 'landed' else slot_status end,
               updated_at = now()
         where work_item_id = new.id and slot = 'default';
        if not found then
            insert into work_migration_reservations
                (work_item_id, slot, number, project, target_repo, target_branch, status)
            values (new.id, 'default', new.migration, coalesce(new.app, ''),
                    'github.com/' || new.migration_repo,
                    nullif(regexp_replace(new.branch_target, '^(refs/heads/|refs/remotes/origin/|origin/)', ''), ''),
                    slot_status);
        end if;
    end if;
    return new;
end $$;

create or replace function ameesh_module_work_guard() returns trigger language plpgsql as $$
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

create or replace function work_item_reviewers_audit() returns trigger
language plpgsql as $$
declare
    name text;
    previous text[];
    remaining text[];
begin
    -- Les chemins Python journalisent déjà le retrait avec leur acteur ; ils
    -- posent ce drapeau pour ne pas doubler la ligne.
    if current_setting('ameesh.reviewer_audit', true) = 'python' then
        return null;
    end if;
    previous := coalesce(old.reviewers, '{}');
    if coalesce(old.reviewer, '') <> ''
       and not old.reviewer = any(previous) then
        previous := array_append(previous, old.reviewer);
    end if;
    remaining := coalesce(new.reviewers, '{}');
    foreach name in array previous loop
        if coalesce(name, '') <> '' and name <> '-none'
           and name = new.assignee
           and not name = any(remaining) then
            insert into work_item_events (work_item_id, state, note, actor)
            values (new.id, new.state,
                    'reviewer ' || name || ' cleared (they now hold the lot)',
                    'db:reviewers_sync');
        end if;
    end loop;
    return null;
end;
$$;


DROP INDEX IF EXISTS work_items_fingerprint_open_uniq;
CREATE UNIQUE INDEX work_items_fingerprint_open_uniq ON work_items (lower(source))
 WHERE source LIKE 'empreinte:%' AND state NOT IN ('merged', 'promoted', 'closed');
ALTER TABLE work_items DROP COLUMN legacy_state;

-- Permit the migration runner to apply 0518 again after an operator rollback.
DELETE FROM schema_migrations WHERE version = 518;
