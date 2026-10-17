-- SPDX-License-Identifier: AGPL-3.0-only
-- 0449_work_milestones_correction — #1693 (N4) : une fusion annulée ne date pas.
--
-- Depuis #1693, une correction `merged → build` ne retire plus le jalon de la
-- fusion annulée : la table garde l'historique (le journal aussi), et la
-- lecture ne retient que le jalon EFFECTIF — celui qu'aucune correction
-- `merged → build` postérieure n'annule. La vraie fusion écrit son propre
-- jalon, postérieur à la correction.
--
-- L'index unique de 0013 ne vaut donc plus que pour `requested` : un lot peut
-- désormais porter plusieurs jalons `merged` (un par fusion). La correction
-- `promoted → merged` ne crée pas de jalon : la fusion réelle a déjà eu lieu.
--
-- Additive (L151) : aucune ligne n'est retirée ni réécrite ; le code d'avant
-- #1693 lit sans casser (il prend le premier jalon `merged`, comme avant).
drop index if exists work_item_milestones_auto_idx;
create unique index if not exists work_item_milestones_requested_idx
    on work_item_milestones (work_item_id) where kind = 'requested';

create or replace function agent_mesh_work_item_milestones() returns trigger
language plpgsql as $$
begin
    if tg_op = 'INSERT' then
        execute format(
            'insert into %I.work_item_milestones (work_item_id, kind, at, note)'
            ' values ($1, ''requested'', $2, ''création'') on conflict do nothing',
            tg_table_schema) using new.id, new.created_at;
    elsif new.state = 'merged' and old.state not in ('merged', 'promoted') then
        execute format(
            'insert into %I.work_item_milestones (work_item_id, kind, at, note)'
            ' values ($1, ''merged'', now(), ''transition'') on conflict do nothing',
            tg_table_schema) using new.id;
    end if;
    return new;
end $$;
