-- SPDX-License-Identifier: AGPL-3.0-only
-- L122 / #443 : un numéro de migration se réserve par dépôt. Jusqu'ici
-- l'index unique portait sur le seul numéro : ameesh et nexlink partageaient
-- une suite, et le plancher venait toujours de src/ameesh/migrations.
-- L'unicité porte désormais sur (dépôt, numéro), garantie par l'index ;
-- un numéro réservé a toujours son dépôt.
alter table work_items add column if not exists migration_repo text;

-- les réservations existantes : le dépôt de l'issue du lot (« owner/repo#N »)
update work_items
   set migration_repo = coalesce(
           substring(issue_ref from '^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#[0-9]+$'),
           'unknown/' || coalesce(nullif(btrim(app), ''), 'none'))
 where migration is not null and migration_repo is null;

-- un client d'avant #443 pose `migration` sans dépôt (et la libère sans
-- l'effacer) : le dépôt de l'issue est pris pour lui, et sa réservation tombe
-- dans la bonne suite pendant la mise à jour des postes.
create or replace function work_items_migration_repo_fill() returns trigger
language plpgsql as $$
begin
    if new.migration is null then
        new.migration_repo := null;
    elsif new.migration_repo is null then
        new.migration_repo := coalesce(
            substring(new.issue_ref from '^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#[0-9]+$'),
            'unknown/' || coalesce(nullif(btrim(new.app), ''), 'none'));
    end if;
    return new;
end $$;

drop trigger if exists work_items_migration_repo_fill on work_items;
create trigger work_items_migration_repo_fill
    before insert or update of migration, migration_repo on work_items
    for each row execute function work_items_migration_repo_fill();

alter table work_items add constraint work_items_migration_repo_check
    check ((migration is null) = (migration_repo is null));

drop index if exists work_items_migration_idx;
create unique index if not exists work_items_migration_repo_idx
    on work_items (migration_repo, migration) where migration is not null;
