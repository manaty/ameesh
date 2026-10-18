-- SPDX-License-Identifier: AGPL-3.0-only
-- 0463_work_milestones_effective_merge — #2317 (N1/N2 de la R1 #1693) :
-- l'horloge de la fusion et la reconnaissance de la correction.
--
-- N1 : le jalon `merged` était daté par now(), début de transaction. Un
-- close_merged retenu par le verrou consultatif — sa transaction a commencé
-- avant la correction, il écrit après elle — datait sa fusion AVANT la
-- correction qui l'annule à la lecture. Le jalon est daté par
-- clock_timestamp(), l'instant réel de l'insertion.
--
-- N2 : une correction `merged → build` se reconnaissait au seul préfixe de sa
-- note ; une note libre du même préfixe annulait une fusion. Elle se reconnaît
-- désormais à la TRANSITION du journal : l'événement `build` dont l'événement
-- précédent du lot est `merged`. Le texte de la note n'est plus un critère.
-- Le prédicat est une fonction, partagée par le déclencheur, les lectures du
-- pilote Postgres et le registre Process.
--
-- Additive (L151) : la fonction du déclencheur 0013/0449 est remplacée, aucune
-- donnée n'est retirée. Les jalons déjà annulés restent dans la table ; la
-- lecture les écarte.

-- La correction doit partager la même horloge que le jalon : now() la
-- daterait avant une fusion écrite dans sa transaction englobante, ou pendant
-- son attente d'un verrou. Ne modifier que le jalon inverse alors leur ordre.
-- Le défaut change pour les écritures futures ; aucun historique réécrit.
alter table work_item_events alter column created_at set default clock_timestamp();

create or replace function agent_mesh_merged_annulled(p_item_id bigint, p_at timestamptz)
returns boolean
language sql
stable
as $$
    select exists (
        select 1 from work_item_events c
         where c.work_item_id = p_item_id
           and c.state = 'build'
           and c.created_at >= p_at
           and (select p.state from work_item_events p
                 where p.work_item_id = c.work_item_id and p.id < c.id
                 order by p.id desc limit 1) = 'merged')
$$;

comment on function agent_mesh_merged_annulled(bigint, timestamptz) is
  '#2317 : une correction merged → build postérieure au jalon l''annule. La correction se reconnaît à la transition du journal, jamais au texte de sa note.';

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
        -- si une correction `merged → build` a annulé le précédent (le
        -- prédicat lit la transition du journal, pas le texte de la note).
        execute format(
            'insert into %1$I.work_item_milestones (work_item_id, kind, at, note)'
            ' select $1, ''merged'', clock_timestamp(), ''transition'''
            '  where not exists (select 1 from %1$I.work_item_milestones m'
            '   where m.work_item_id = $1 and m.kind = ''merged'''
            '     and not %1$I.agent_mesh_merged_annulled($1, m.at))',
            tg_table_schema) using new.id;
    end if;
    return new;
end $$;
