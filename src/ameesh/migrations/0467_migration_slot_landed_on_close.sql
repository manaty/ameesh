-- SPDX-License-Identifier: AGPL-3.0-only
-- #879 N (#2658) : un numero de migration atterri ne redevient jamais
-- `released`.
--
-- La transition `merged -> closed` rejoue la projection avec
-- `work_items.migration` encore pose : l'etat `closed` donnait au creneau
-- `default` le statut `released`, alors que le numero avait atterri sur la
-- cible. La garde posee par 0432 ne preservait `landed` que pour une
-- projection `reserved` (retour `merged -> blocked`) ; elle couvre aussi
-- `released` : un `landed` de meme numero le reste quel que soit l'etat du
-- lot, un nouveau numero suit son statut normal. La ligne d'atterrissage ne
-- se perd donc pas et le numero consomme n'est pas rouvert.
--
-- Copie complete de la definition posee par 0432 : l'ordre d'arrivee des
-- migrations non appliquees n'est pas garanti, donc celle-ci porte aussi les
-- deux regles de #879/#1022. La derniere appliquee laisse le meme etat.
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
