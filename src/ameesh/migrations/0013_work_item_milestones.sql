-- 0013_work_item_milestones — les jalons d'un lot (décision 0018 point 5, R19).
--
-- La frise « demande → gel → revue → fusion » est **écrite**, pas déduite d'un
-- état : un lot peut être gelé, recevoir un verdict `blocked`, être regelé, etc.
--
--   requested  la création du lot — automatique (l'insertion dans work_items) ;
--   frozen     la branche est gelée pour la revue — explicite
--              (`ameesh work milestone <id> frozen --sha S`) ;
--   verdict    le verdict de revue, `ok` ou `blocked` — explicite
--              (`ameesh work milestone <id> verdict ok --sha S [--note …]`) ;
--   merged     l'entrée en `merged` — automatique (la transition d'état).
--
-- Les jalons automatiques passent par un trigger sur `work_items`, donc tout
-- écrivain des lots (orchestrateur, Nexlink) les produit sans le savoir ;
-- `frozen` et `verdict` ne se déduisent pas d'un état, ils sont déclarés. Les
-- durées (demande → gel, gel → premier verdict, gel → fusion, verdicts bloqués)
-- se calculent dans la couche de stockage, pas ici.

create table if not exists work_item_milestones (
    id           bigserial primary key,
    work_item_id bigint not null references work_items(id) on delete cascade,
    kind         text not null check (kind in ('requested', 'frozen', 'verdict', 'merged')),
    at           timestamptz not null default now(),
    sha          text not null default '',
    actor        text not null default '',
    verdict      text,
    note         text not null default '',
    -- le verdict n'existe que pour un verdict, et un verdict en a toujours un
    check ((kind = 'verdict') = (verdict is not null)),
    check (verdict is null or verdict in ('ok', 'blocked'))
);

create index if not exists work_item_milestones_item_idx
    on work_item_milestones (work_item_id, at);

-- Un seul jalon automatique par lot et par kind : le trigger peut se déclencher
-- plusieurs fois (entrée en `merged` après un retour en build) sans doublon.
create unique index if not exists work_item_milestones_auto_idx
    on work_item_milestones (work_item_id, kind)
    where kind in ('requested', 'merged');

-- `tg_table_schema` : la table est nommée explicitement dans le schéma du lot,
-- parce que la session qui écrit peut avoir un autre `search_path` (les tests et
-- le registre vivent chacun dans leur schéma).
create or replace function agent_mesh_work_item_milestones() returns trigger
language plpgsql as $$
begin
    if tg_op = 'INSERT' then
        execute format(
            'insert into %I.work_item_milestones (work_item_id, kind, at, note)'
            ' values ($1, ''requested'', $2, ''création'') on conflict do nothing',
            tg_table_schema) using new.id, new.created_at;
    elsif new.state = 'merged' and old.state is distinct from 'merged' then
        execute format(
            'insert into %I.work_item_milestones (work_item_id, kind, at, note)'
            ' values ($1, ''merged'', now(), ''transition'') on conflict do nothing',
            tg_table_schema) using new.id;
    end if;
    return new;
end $$;

drop trigger if exists work_items_milestones on work_items;
create trigger work_items_milestones
    after insert or update of state on work_items
    for each row execute function agent_mesh_work_item_milestones();

-- Reprise : les lots déjà ouverts ont leur `requested` (et leur `merged` s'ils
-- le sont) ; les jalons gel/verdict du passé ne se devinent pas, ils manquent.
insert into work_item_milestones (work_item_id, kind, at, note)
select w.id, 'requested', w.created_at, 'reprise 0013' from work_items w
on conflict do nothing;

insert into work_item_milestones (work_item_id, kind, at, note)
select e.work_item_id, 'merged', min(e.created_at), 'reprise 0013'
  from work_item_events e
 where e.state = 'merged'
 group by e.work_item_id
on conflict do nothing;

comment on table work_item_milestones is
  'L10 (R19) : les jalons d''un lot — requested et merged automatiques (trigger), frozen et verdict déclarés par la CLI. Une ligne par jalon, dans l''ordre où ils arrivent.';
