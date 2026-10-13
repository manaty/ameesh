-- SPDX-License-Identifier: AGPL-3.0-only
-- #1022: une fusion solde son creneau en `landed`, jamais en `released`.
--
-- Le declencheur de 0419 marque le creneau legacy `default` en `released` des
-- que `work_items.migration` passe a NULL, sans regarder l'etat du lot. #956
-- remet la projection a NULL dans la transaction meme de la transition vers
-- `merged` ; quand les deux ecritures tiennent en UNE instruction — ou par un
-- client anterieur a 0419 — le creneau finissait `released` alors que le lot a
-- fusionne et que sa migration a atterri sur la cible. L'etat tranche
-- desormais : un etat terminal d'atterrissage (`merged`, `promoted`) lande le
-- creneau ; tout le reste (lot ouvert, `closed`) le libere sans atterrissage.
create or replace function work_items_migration_slot_sync() returns trigger
language plpgsql as $$
begin
    if new.migration is null then
        update work_migration_reservations
           set status = case when new.state in ('merged', 'promoted') then 'landed'
                             else 'released' end,
               updated_at = now()
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
