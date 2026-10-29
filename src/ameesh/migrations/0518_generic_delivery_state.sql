-- SPDX-License-Identifier: AGPL-3.0-only
-- ameesh: non-additive
-- L99 T5 (#4353), non-additive. Owner decision #164165, approval #167.
-- Apply with executors stopped; relaunch only after the transaction commits.
-- Historical events/receipts and signed Process artifacts are not rewritten.
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS legacy_state text;
ALTER TABLE work_items DROP CONSTRAINT IF EXISTS work_items_state_check;
ALTER TABLE work_items DROP CONSTRAINT IF EXISTS work_items_workflow_stage_needs_ref;

-- Install the new predicate before rewriting rows: delivered fingerprints
-- must never become open transiently and collide with their recurrence.
DROP INDEX IF EXISTS work_items_fingerprint_open_uniq;
CREATE UNIQUE INDEX work_items_fingerprint_open_uniq ON work_items (lower(source))
 WHERE source LIKE 'empreinte:%'
   AND state NOT IN ('merged', 'promoted', 'delivered', 'closed');

CREATE OR REPLACE FUNCTION work_items_generic_state_sync() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state IN ('merged', 'promoted') THEN
        NEW.legacy_state := NEW.state;
        IF coalesce(NEW.workflow_ref, '') = '' THEN
            NEW.workflow_stage := NEW.state;
        END IF;
        NEW.state := 'delivered';
    ELSIF NEW.state = 'delivered' THEN
        IF coalesce(NEW.workflow_ref, '') = '' THEN
            IF NEW.workflow_stage NOT IN ('merged', 'promoted')
               OR NEW.workflow_stage IS NULL THEN
                RAISE EXCEPTION 'delivered requires a development delivery stage';
            END IF;
            NEW.legacy_state := NEW.workflow_stage;
        ELSIF NEW.legacy_state IS NULL THEN
            NEW.legacy_state := 'merged';
        END IF;
    ELSE
        NEW.legacy_state := NEW.state;
        IF TG_OP = 'UPDATE' AND coalesce(NEW.workflow_ref, '') = ''
           AND OLD.state = 'delivered'
           AND NEW.workflow_stage IS NOT DISTINCT FROM OLD.workflow_stage THEN
            NEW.workflow_stage := NULL;
        END IF;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS aa_work_items_generic_state_sync ON work_items;
CREATE TRIGGER aa_work_items_generic_state_sync BEFORE INSERT OR UPDATE ON work_items
 FOR EACH ROW EXECUTE FUNCTION work_items_generic_state_sync();

create or replace function work_items_migration_slot_sync() returns trigger
language plpgsql as $$
declare
    slot_status text;
begin
    if new.migration is null then
        update work_migration_reservations
           set status = case when new.state in ('merged', 'promoted', 'delivered') then 'landed'
                             else 'released' end,
               updated_at = now()
         where work_item_id = new.id and slot = 'default' and status = 'reserved';
    else
        slot_status := case when new.state in ('merged', 'promoted', 'delivered') then 'landed'
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

create or replace function agent_mesh_work_item_milestones() returns trigger
language plpgsql as $$
begin
    if tg_op = 'INSERT' then
        execute format(
            'insert into %I.work_item_milestones (work_item_id, kind, at, note)'
            ' values ($1, ''requested'', $2, ''création'') on conflict do nothing',
            tg_table_schema) using new.id, new.created_at;
    elsif new.state = 'delivered' and new.legacy_state = 'merged'
       and old.state not in ('merged', 'promoted', 'delivered') then
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

-- Normalization runs before module policy checks (trigger name aa_*).
-- Enforce both the core rule and the historical stage rule, including
-- promotion between two stages that share the same core.
CREATE OR REPLACE FUNCTION ameesh_module_work_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_phase text;
    new_phase text := CASE WHEN NEW.state = 'delivered' THEN NEW.legacy_state ELSE NEW.state END;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        old_phase := CASE WHEN OLD.state = 'delivered' THEN OLD.legacy_state ELSE OLD.state END;
        IF OLD.type NOT IN ('bug','evolution','improvement')
           AND (OLD.type, OLD.app, OLD.workstream)
               IS DISTINCT FROM (NEW.type, NEW.app, NEW.workstream) THEN
            RAISE EXCEPTION 'module work item identity is immutable';
        END IF;
    END IF;
    IF TG_OP = 'INSERT' OR old_phase IS DISTINCT FROM new_phase
       OR (OLD.type, OLD.app, OLD.workstream)
          IS DISTINCT FROM (NEW.type, NEW.app, NEW.workstream) THEN
        PERFORM ameesh_module_requirements(NEW, NEW.state, NULL, NULL);
        IF new_phase IS DISTINCT FROM NEW.state THEN
            PERFORM ameesh_module_requirements(NEW, new_phase, NULL, NULL);
        END IF;
    ELSE
        PERFORM ameesh_module_requirements(NEW, NULL, NULL, NULL);
    END IF;
    RETURN NEW;
END $$;

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
            values (new.id, case when new.state = 'delivered' then new.legacy_state else new.state end,
                    'reviewer ' || name || ' cleared (they now hold the lot)',
                    'db:reviewers_sync');
        end if;
    end loop;
    return null;
end;
$$;


-- No event is appended for this representation change. Existing steps of
-- pinned Process lots are preserved, including custom names.
UPDATE work_items SET state = state WHERE legacy_state IS NULL;
ALTER TABLE work_items ADD CONSTRAINT work_items_state_check
 CHECK (state IN ('intake', 'build', 'qa', 'validated', 'delivered',
                  'blocked', 'waiting_human', 'closed'));
ALTER TABLE work_items ADD CONSTRAINT work_items_workflow_stage_needs_ref
 CHECK (workflow_stage IS NULL OR workflow_ref IS NOT NULL
        OR (state = 'delivered' AND workflow_stage IN ('merged', 'promoted')));
ALTER TABLE work_items ADD CONSTRAINT work_items_legacy_state_matches
 CHECK (((state = 'delivered' AND legacy_state IN ('merged', 'promoted'))
         OR (state <> 'delivered' AND legacy_state = state)) IS TRUE);
