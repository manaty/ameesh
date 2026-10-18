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
    elsif new.state = 'merged' and old.state is distinct from 'merged' then
        -- Un jalon par fusion EFFECTIVE : un retour en merged depuis
        -- promoted, blocked ou waiting_human n'en écrit pas un second, sauf
        -- si une correction `merged → build` a annulé le précédent.
        execute format(
            'insert into %1$I.work_item_milestones (work_item_id, kind, at, note)'
            ' select $1, ''merged'', now(), ''transition'''
            '  where not exists (select 1 from %1$I.work_item_milestones m'
            '   where m.work_item_id = $1 and m.kind = ''merged'''
            '     and not exists (select 1 from %1$I.work_item_events c'
            '      where c.work_item_id = $1 and c.state = ''build'''
            '        and starts_with(c.note, ''correction: merged → build'')'
            '        and c.created_at >= m.at))',
            tg_table_schema) using new.id;
    end if;
    return new;
end $$;

-- Reprise additive : sous l'index unique de 0013, la refusion d'un lot
-- corrigé `merged → build` (depuis le 13/10) n'a pas écrit de jalon ; le seul
-- jalon restant est celui que la correction annule. On date la vraie fusion
-- par la première entrée en merged du journal après la dernière correction.
insert into work_item_milestones (work_item_id, kind, at, note)
select c.work_item_id, 'merged', min(e.created_at), 'reprise 0449'
  from (select work_item_id, max(created_at) as at from work_item_events
         where state = 'build' and starts_with(note, 'correction: merged → build')
         group by work_item_id) c
  join work_item_events e on e.work_item_id = c.work_item_id
                         and e.state = 'merged' and e.created_at > c.at
 where not exists (select 1 from work_item_milestones m
                    where m.work_item_id = c.work_item_id and m.kind = 'merged'
                      and m.at > c.at)
 group by c.work_item_id;
