-- 0004_work_items — la machine à états des lots, reprise d'un orchestrateur de tickets existant (spec §5 point 4).
--
--   intake → build → qa → merged → promoted
-- plus `blocked` et `waiting_human` (les deux états d'attente de cet orchestrateur).
-- La boucle qa → build est bornée par `loops` (2 allers-retours, comme dans le
-- design d'origine) : c'est la CLI qui refuse le troisième, la base garde la trace.

create table if not exists work_items (
    id          bigserial primary key,
    type        text not null default 'evolution'
                check (type in ('bug','evolution')),
    source      text not null default '',
    app         text not null default '',
    title       text not null,
    body        text not null default '',
    issue_ref   text,
    workstream  text,
    state       text not null default 'intake'
                check (state in ('intake','build','qa','merged','promoted',
                                 'blocked','waiting_human')),
    assignee    text,
    loops       integer not null default 0,
    budget_usd  numeric(12,4),
    spent_usd   numeric(12,4) not null default 0,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    closed_at   timestamptz
);

create index if not exists work_items_state_idx on work_items (state, updated_at desc);
create index if not exists work_items_assignee_idx on work_items (assignee) where assignee is not null;

-- Le journal des transitions : qui a bougé quoi, quand, pourquoi.
create table if not exists work_item_events (
    id           bigserial primary key,
    work_item_id bigint not null references work_items(id) on delete cascade,
    state        text not null,
    note         text not null default '',
    actor        text not null default '',
    created_at   timestamptz not null default now()
);

create index if not exists work_item_events_item_idx on work_item_events (work_item_id, id);

-- Un changement d'état réveille le board et, plus tard, les abonnés Nexlink :
-- même mécanisme que la boîte aux lettres (LISTEN/NOTIFY), canal `work_item`.
create or replace function agent_mesh_notify_work_item() returns trigger
language plpgsql as $$
begin
    perform pg_notify('work_item', json_build_object(
        'id', new.id, 'state', new.state, 'title', left(new.title, 80))::text);
    return new;
end $$;

drop trigger if exists work_items_notify on work_items;
create trigger work_items_notify
    after insert or update of state on work_items
    for each row execute function agent_mesh_notify_work_item();
