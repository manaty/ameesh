-- SPDX-License-Identifier: AGPL-3.0-only
-- L148 (#147) stage 3, migration A : `agent_registry.chantier` devient
-- `workstream`. La colonne est ajoutée et remplie depuis `chantier`, et un
-- déclencheur maintient les deux en phase dans les deux sens : le code d'avant
-- (qui ne connaît que `chantier`) et celui d'après (qui lira `workstream`)
-- tournent sur le même schéma. La migration D, plus tard, retirera le
-- déclencheur puis `chantier` (plan L148a §4, stage 3).
alter table agent_registry add column if not exists workstream text not null default '';

-- remplissage : chaque ligne existante garde son chantier
update agent_registry set workstream = chantier where workstream is distinct from chantier;

-- les deux colonnes restent égales : celle qui est écrite est recopiée sur
-- l'autre. Quand un même ordre écrit les deux (les deux valeurs diffèrent de
-- l'ancienne), le nouveau nom l'emporte — un ordre SQL ne dit pas lequel est
-- le dernier.
create or replace function agent_registry_workstream_sync() returns trigger
language plpgsql as $$
begin
    if tg_op = 'INSERT' then
        if new.workstream = '' and new.chantier <> '' then
            new.workstream := new.chantier;
        else
            new.chantier := new.workstream;
        end if;
    elsif new.chantier is distinct from old.chantier
      and new.workstream is distinct from old.workstream then
        new.chantier := new.workstream;
    elsif new.chantier is distinct from old.chantier then
        new.workstream := new.chantier;
    elsif new.workstream is distinct from old.workstream then
        new.chantier := new.workstream;
    end if;
    return new;
end $$;

drop trigger if exists agent_registry_workstream_sync on agent_registry;
create trigger agent_registry_workstream_sync
    before insert or update of chantier, workstream on agent_registry
    for each row execute function agent_registry_workstream_sync();
