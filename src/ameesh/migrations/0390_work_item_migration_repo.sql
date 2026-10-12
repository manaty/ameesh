-- SPDX-License-Identifier: AGPL-3.0-only
-- L122 / #443 : un numéro de migration se réserve par dépôt. Jusqu'ici
-- l'index unique portait sur le seul numéro : ameesh et nexlink partageaient
-- une suite, et le plancher venait toujours de src/ameesh/migrations.
-- L'unicité porte désormais sur (dépôt, numéro), garantie par l'index ;
-- un numéro réservé a toujours son dépôt, en minuscules (`owner/repo`).
alter table work_items add column if not exists migration_repo text;

-- le dépôt d'une issue « owner/repo#N » (espaces retirés, minuscules), comme
-- `work.lot_repository` ; NULL si l'issue n'a pas cette forme
create or replace function work_items_issue_repo(issue text) returns text
language sql immutable as $$
    select lower(substring(btrim(issue, E' \t\n\r\f\v')
                           from '^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#[0-9]+$'))
$$;

-- les réservations existantes : leur dépôt s'il est déjà donné (ramené en
-- minuscules, un dépôt vide n'en est pas un), sinon celui de l'issue du lot.
-- Aucun espace de noms inventé : une réservation sans dépôt connu arrête la
-- migration (le lot se rattache par `ameesh work link`, ou la réservation se
-- libère, avant).
update work_items
   set migration_repo = nullif(lower(btrim(migration_repo, E' \t\n\r\f\v')), '')
 where migration_repo is not null;
do $$
declare orphans text;
begin
    select string_agg('#' || id || ' (' || migration || ')', ', ' order by id) into orphans
      from work_items
     where migration is not null and migration_repo is null
       and work_items_issue_repo(issue_ref) is null;
    if orphans is not null then
        raise exception 'migration 0390: reserved migration numbers without a repository '
                        '(issue owner/repo#N): %; link the issues or release them first', orphans;
    end if;
end $$;
update work_items set migration_repo = work_items_issue_repo(issue_ref)
 where migration is not null and migration_repo is null;
update work_items set migration_repo = null
 where migration is null and migration_repo is not null;

-- un client d'avant #443 pose `migration` sans dépôt (et la libère sans
-- l'effacer) : le dépôt de l'issue est pris pour lui, et sa réservation tombe
-- dans la bonne suite pendant la mise à jour des postes ; sans issue, refus.
-- Un dépôt donné est ramené en minuscules : une seule suite par dépôt.
create or replace function work_items_migration_repo_fill() returns trigger
language plpgsql as $$
begin
    if new.migration is null then
        new.migration_repo := null;
    elsif nullif(btrim(new.migration_repo, E' \t\n\r\f\v'), '') is not null then
        new.migration_repo := lower(btrim(new.migration_repo, E' \t\n\r\f\v'));
    else
        new.migration_repo := work_items_issue_repo(new.issue_ref);
        if new.migration_repo is null then
            raise exception 'lot #%: migration % without a repository: link the lot''s issue '
                            '(owner/repo#N) first', new.id, new.migration;
        end if;
    end if;
    return new;
end $$;

drop trigger if exists work_items_migration_repo_fill on work_items;
create trigger work_items_migration_repo_fill
    before insert or update of migration, migration_repo on work_items
    for each row execute function work_items_migration_repo_fill();

alter table work_items add constraint work_items_migration_repo_check
    check ((migration is null) = (migration_repo is null)
           and migration_repo = lower(migration_repo));

drop index if exists work_items_migration_idx;
create unique index if not exists work_items_migration_repo_idx
    on work_items (migration_repo, migration) where migration is not null;
