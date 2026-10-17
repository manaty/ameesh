-- SPDX-License-Identifier: AGPL-3.0-only
-- L122/#1567 : le numéro réservé n'est pas toujours celui qui atterrit — un
-- paquet renumérote au max+1 de la cible (452 au lieu de la réservation 457
-- de #1240). La réservation (`migration`, `migration_repo`) est une intention,
-- libérée à l'état terminal (#956) ; le numéro atterri est un fait, gardé à
-- part pour que `work show` le nomme.
alter table work_items add column if not exists migration_landed integer;
alter table work_items add column if not exists migration_landed_repo text;

alter table work_items drop constraint if exists work_items_migration_landed_check;
alter table work_items add constraint work_items_migration_landed_check
    check ((migration_landed is null) = (migration_landed_repo is null)
           and (migration_landed is null
                or (migration_landed between 1 and 9999
                    and btrim(migration_landed_repo) <> ''
                    and migration_landed_repo = lower(migration_landed_repo))));

-- deux lots d'un même dépôt ne nomment jamais le même numéro atterri
create unique index if not exists work_items_migration_landed_idx
    on work_items (migration_landed_repo, migration_landed)
 where migration_landed is not null;
