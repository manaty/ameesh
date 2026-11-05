-- SPDX-License-Identifier: AGPL-3.0-only
-- #5869 : un drainage manuel en cours garde son intention à travers une perte.
-- Une ligne `lost` issue de `draining` le dit ; le reconcile la ramène alors à
-- `draining` (même génération, même échéance) au lieu de la rouvrir.
set local lock_timeout = '5s';
alter table host_lifecycle
    add column if not exists lost_from_drain boolean not null default false;

-- ameesh: trigger compatible with previous code
-- Pour le code N-1, le trigger ne change qu'une chose : `return_at` reste posé
-- sur une ligne `lost` issue de `draining`. Aucun chemin N-1 ne le lit dans cet
-- état. expire_returns ne lit que `draining`, undrain refuse `lost`, drain rend
-- la ligne telle quelle, et le reconcile N-1 remet `return_at=NULL`.
-- La capture vit dans la base, pas dans le détecteur : un détecteur N-1 qui
-- écrit encore `return_at=NULL` au passage draining → lost (coexistence des
-- versions pendant un déploiement) garde lui aussi l'échéance et l'intention.
create or replace function ameesh_host_lost_drain_intent() returns trigger
language plpgsql as $$
begin
    if new.mode = 'lost' and old.mode = 'draining' then
        new.lost_from_drain := true;
        new.return_at := old.return_at;
    elsif new.mode = 'lost' and old.mode <> 'lost' then
        new.lost_from_drain := false;
    end if;
    return new;
end;
$$;

drop trigger if exists aa_host_lost_drain_intent on host_lifecycle;
create trigger aa_host_lost_drain_intent
    before update of mode on host_lifecycle
    for each row execute function ameesh_host_lost_drain_intent();
