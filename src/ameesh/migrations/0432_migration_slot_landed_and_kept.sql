-- SPDX-License-Identifier: AGPL-3.0-only
-- #879 (suite #711) : le declencheur de creneau porte les deux regles en
-- entier — une fusion lande son creneau meme quand `work_items.migration`
-- repasse a NULL, et un numero deja atterri ne redevient jamais `reserved`.
--
-- Cette migration est la copie de #879 de la definition posee par 0420 :
-- l'ordre d'arrivee des migrations non appliquees n'est pas garanti, donc
-- chacune des deux porte les DEUX regles en entier. La derniere appliquee
-- laisse le meme etat.
--
-- #1022 : #956 remet la projection a NULL dans la transaction meme de la
-- transition vers `merged` ; quand les deux ecritures tiennent en UNE
-- instruction — ou par un client anterieur a 0419 — le creneau finissait
-- `released` alors que le lot a fusionne et que sa migration a atterri sur la
-- cible. L'etat tranche desormais : un etat terminal d'atterrissage (`merged`,
-- `promoted`) lande le creneau ; tout le reste (lot ouvert, `closed`) le
-- libere sans atterrissage.
--
-- #879 : `merged -> blocked` rejoue la projection avec `migration` encore
-- pose. La ligne existante est mise a jour — jamais reinseree — et un
-- `landed` de meme numero le reste : un numero consomme ne se rouvre pas (le
-- 23505 brut de l'index partiel disparait). Un nouveau numero, lui, suit son
-- statut normal (`reserved`).
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
        update work_migration_reservations
           set number = new.migration, project = coalesce(new.app, ''),
               target_repo = 'github.com/' || new.migration_repo,
               target_branch = nullif(regexp_replace(new.branch_target, '^(refs/heads/|refs/remotes/origin/|origin/)', ''), ''),
               status = case when work_migration_reservations.status = 'landed'
                              and slot_status = 'reserved'
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
