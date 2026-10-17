-- SPDX-License-Identifier: AGPL-3.0-only
-- #2204 (N1/N2 de #2076) : le retrait automatique d'un relecteur devenu
-- assigné a sa ligne de journal, même par SQL direct ; re-désigner un nom
-- déjà présent le remet en dernier.
--
-- Limite connue : le backfill de 0456 a retiré les noms avant ce trigger et
-- la ligne ne les porte plus (reviewer remis à ''). Ces retraits historiques
-- ne sont pas reconstituables ; tout retrait postérieur est journalisé.

create or replace function work_item_reviewers_sync() returns trigger
language plpgsql as $$
declare
    names text[];
    canonical text[] := '{}';
    name text;
begin
    names := coalesce(new.reviewers, '{}');
    if tg_op = 'INSERT' then
        if new.reviewer = '-none' then
            names := array['-none'];
        else
            names := array_append(names, new.reviewer);
        end if;
    elsif new.reviewers is not distinct from old.reviewers then
        -- Legacy writers only know reviewer. A designation adds a name;
        -- clearing it withdraws that name, preserving the other reviewers.
        if new.reviewer is distinct from old.reviewer then
            if new.reviewer = '-none' then
                -- #1979: a waiver replaces every designation, including when
                -- it is written by a runner that knows only reviewer.
                names := array['-none'];
            elsif coalesce(new.reviewer, '') = '' then
                names := array_remove(names, old.reviewer);
            else
                -- #2204/N2 : un nom déjà désigné repasse en dernier, pour que
                -- le champ legacy `reviewer` désigne bien le dernier désigné
                -- (l'ancien array_append + dédoublonnage gardait sa place).
                names := array_append(
                    array_remove(array_remove(names, '-none'), new.reviewer),
                    new.reviewer);
            end if;
        end if;
    end if;
    foreach name in array names loop
        if coalesce(name, '') <> '' and name <> '-none'
           and name is distinct from new.assignee
           and not name = any(canonical) then
            canonical := array_append(canonical, name);
        end if;
    end loop;
    -- Keep the waiver only on its own; it is never a designated reviewer.
    if cardinality(canonical) = 0 and '-none' = any(names) then
        canonical := array['-none'];
    end if;
    new.reviewers := canonical;
    new.reviewer := coalesce(canonical[cardinality(canonical)], '');
    return new;
end;
$$;

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

drop trigger if exists work_item_reviewers_audit on work_items;
create trigger work_item_reviewers_audit
after update of reviewer, reviewers, assignee on work_items
for each row execute function work_item_reviewers_audit();
