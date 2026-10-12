-- SPDX-License-Identifier: AGPL-3.0-only
-- 0396_ameesh_events — the mesh's event log, source of `GET /api/v1/events`
-- (L85, lot #373; embeddable UI study docs/design/etudes/ui-integrable.md §2.1).
--
-- Append-only: an increasing `id`, a `type` (`lot.updated`, `turn.started`…),
-- the `object` it is about (`lot:373`, `agent:dev3`, `action:act_…`,
-- `thread:demo/373`), its `project` (the filter of L86's authorisations) and a
-- short `data` (identifiers and states, never free text: the client rereads
-- the object through the API).
--
-- Written by triggers on the tables it describes, hence in the SAME
-- transaction as the change, with psql as with psycopg: a rolled-back change
-- leaves no event. An insertion emits `pg_notify('ameesh_events', id)`, the
-- identifier alone (8000-byte limit of NOTIFY, nothing sensitive in the
-- channel).
--
-- Reading order: `id` is taken at insertion, not at commit. `tx` (the
-- writing transaction) gives the order of a reader: it reads only the
-- events of `tx < pg_snapshot_xmin(pg_current_snapshot())` (every such
-- transaction is over: committed or rolled back), by (tx, id), after its
-- cursor (tx, id). No hole, no timeout; a long open transaction delays the
-- delivery, it never loses an event.
--
-- Neither updated nor deleted, except by the retention (0033 §9: one year by
-- default, `ameesh events prune`): a row younger than 30 days is never
-- deleted.

create table if not exists ameesh_events (
    id          bigserial primary key,
    type        text not null check (type ~ '^[a-z]+\.[a-z_]+$'),
    object      text not null check (length(object) between 1 and 300),
    project     text,
    data        jsonb not null default '{}'::jsonb check (jsonb_typeof(data) = 'object'),
    tx          xid8 not null default pg_current_xact_id(),
    -- finite and never in the future (guard below): an `infinity` would
    -- never expire, and stay the oldest row for good
    created_at  timestamptz not null default now() check (isfinite(created_at))
);
create index if not exists ameesh_events_created_idx on ameesh_events (created_at);
create index if not exists ameesh_events_tx_idx on ameesh_events (tx, id);

create or replace function ameesh_events_notify() returns trigger
language plpgsql as $$
begin
    perform pg_notify('ameesh_events', new.id::text);
    return new;
end $$;

drop trigger if exists ameesh_events_notify on ameesh_events;
create trigger ameesh_events_notify
    after insert on ameesh_events
    for each row execute function ameesh_events_notify();

create or replace function ameesh_events_guard() returns trigger
language plpgsql as $$
begin
    if tg_op = 'UPDATE' then
        raise exception 'ameesh_events is append-only';
    end if;
    if old.created_at > now() - interval '30 days' then
        raise exception 'ameesh_events: an event younger than 30 days is kept (retention)';
    end if;
    return old;
end $$;

drop trigger if exists ameesh_events_guard on ameesh_events;
create trigger ameesh_events_guard
    before update or delete on ameesh_events
    for each row execute function ameesh_events_guard();

create or replace function ameesh_events_dated() returns trigger
language plpgsql as $$
begin
    if new.created_at > now() + interval '1 day' then
        raise exception 'ameesh_events: created_at in the future (%)', new.created_at;
    end if;
    return new;
end $$;

drop trigger if exists ameesh_events_dated on ameesh_events;
create trigger ameesh_events_dated
    before insert on ameesh_events
    for each row execute function ameesh_events_dated();

-- The retention's watermark: the greatest position (tx, id) it ever deleted,
-- written in the SAME statement as the deletion. A reader whose position is
-- below it may have lost events (reset); at or above it, it lost none. The
-- deletion goes by `created_at`, whatever the order of the rows kept.
create table if not exists ameesh_events_pruned (
    one         boolean primary key default true check (one),
    tx          xid8 not null,
    id          bigint not null,
    at          timestamptz not null default now()
);

-- The identity under which a position (tx, id) means something: the
-- cluster (`system_identifier`) and its timeline, which a point-in-time
-- recovery or a promotion changes (xids start again from the recovery
-- point). The WAL insertion timeline is current at once after a promotion
-- (the checkpoint's lags until the next checkpoint); a standby has none, it
-- gives its checkpoint's. NULL when the functions are refused: every resume
-- is then a reset.
create or replace function ameesh_events_cluster() returns text
language plpgsql stable as $$
declare
    timeline text;
begin
    begin
        timeline := substr(pg_walfile_name(pg_current_wal_lsn()), 1, 8);
    exception when object_not_in_prerequisite_state then   -- in recovery
        timeline := lpad(to_hex((select timeline_id from pg_control_checkpoint())), 8, '0');
    end;
    return (select to_hex(system_identifier) from pg_control_system()) || 't'
        || lower(timeline);
exception when insufficient_privilege then
    return null;
end $$;

-- the event, written by every trigger below (and by `events.record`)
create or replace function ameesh_event(p_type text, p_object text, p_project text,
                                        p_data jsonb) returns void
language sql as $$
    -- `object` cut to the table's bound: an event never refuses the change
    -- it describes (a thread of a 301-character project stays writable)
    insert into ameesh_events (type, object, project, data)
    values (p_type, left(p_object, 300), nullif(p_project, ''), coalesce(p_data, '{}'::jsonb));
$$;

-- the project of an agent: its team, otherwise its worksite (projects.agent_project)
create or replace function ameesh_agent_project(p_agent text) returns text
language sql stable as $$
    select coalesce(nullif(team, ''), nullif(chantier, ''))
      from agent_registry where name = p_agent;
$$;

-- 1. lots: every transition, note or assignment journalled in work_item_events
create or replace function ameesh_events_lot() returns trigger
language plpgsql as $$
declare
    w record;
begin
    select app, workstream, assignee into w from work_items where id = new.work_item_id;
    perform ameesh_event('lot.updated', 'lot:' || new.work_item_id,
        coalesce(nullif(w.app, ''), nullif(w.workstream, ''),
                 ameesh_agent_project(w.assignee)),
        jsonb_build_object('lot', new.work_item_id, 'state', new.state,
                           'actor', new.actor, 'assignee', w.assignee));
    return new;
end $$;

drop trigger if exists ameesh_events_lot on work_item_events;
create trigger ameesh_events_lot
    after insert on work_item_events
    for each row execute function ameesh_events_lot();

-- 2. agents: turn start and end (status running), lease, mode, stop
create or replace function ameesh_events_agent() returns trigger
language plpgsql as $$
declare
    kind text;
    previous text;
begin
    previous := case when tg_op = 'UPDATE' then old.status end;
    if new.status = 'running' and previous is distinct from 'running' then
        kind := 'turn.started';
    elsif previous = 'running' and new.status <> 'running' then
        kind := 'turn.ended';
    else
        kind := 'agent.updated';
    end if;
    perform ameesh_event(kind, 'agent:' || new.name,
        coalesce(nullif(new.team, ''), nullif(new.chantier, '')),
        jsonb_build_object('agent', new.name, 'status', new.status, 'previous', previous,
                           'host', new.host, 'leased', new.lease_owner is not null,
                           'mode', new.mode, 'lot', new.session_work_item));
    return new;
end $$;

-- `update of` fires even on an unchanged value: the update must change something
create trigger ameesh_events_agent_insert
    after insert on agent_registry
    for each row execute function ameesh_events_agent();
create trigger ameesh_events_agent
    after update of status, lease_owner, mode, stop_reason on agent_registry
    for each row
    when (old.status is distinct from new.status
          or old.lease_owner is distinct from new.lease_owner
          or old.mode is distinct from new.mode
          or old.stop_reason is distinct from new.stop_reason)
    execute function ameesh_events_agent();

-- 3. cost: each priced turn
create or replace function ameesh_events_cost() returns trigger
language plpgsql as $$
begin
    perform ameesh_event('cost.recorded', 'agent:' || new.agent,
        ameesh_agent_project(new.agent),
        jsonb_build_object('agent', new.agent, 'turn', new.turn, 'usd', new.usd,
                           'harness', new.harness));
    return new;
end $$;

drop trigger if exists ameesh_events_cost on turn_costs;
create trigger ameesh_events_cost
    after insert on turn_costs
    for each row execute function ameesh_events_cost();

-- 4. threads: each entry (the index is updated at every write)
create or replace function ameesh_events_thread() returns trigger
language plpgsql as $$
begin
    perform ameesh_event('thread.entry',
        'thread:' || new.project || case when new.lot <> '' then '/' || new.lot else '' end,
        new.project,
        jsonb_build_object('project', new.project, 'lot', nullif(new.lot, ''),
                           'entry', new.last_entry_id, 'author', new.last_author));
    return new;
end $$;


create trigger ameesh_events_thread_insert
    after insert on thread_index
    for each row execute function ameesh_events_thread();
create trigger ameesh_events_thread
    after update of entries on thread_index
    for each row
    when (old.entries is distinct from new.entries)
    execute function ameesh_events_thread();

-- 5. decisions: an action proposed, its state changes, a receipt attached
create or replace function ameesh_events_action() returns trigger
language plpgsql as $$
declare
    data jsonb;
begin
    data := jsonb_build_object('action', new.action_id, 'state', new.state,
                               'operation', new.operation, 'class', new.class,
                               'lot', new.work_item);
    if tg_op = 'INSERT' then
        perform ameesh_event('decision.created', 'action:' || new.action_id, new.project, data);
        return new;
    end if;
    if old.state is distinct from new.state then
        perform ameesh_event(case when old.state = 'proposed' then 'decision.closed'
                                  else 'action.updated' end,
                             'action:' || new.action_id, new.project,
                             data || jsonb_build_object('previous', old.state));
    end if;
    if old.auth_receipt is null and new.auth_receipt is not null then
        perform ameesh_event('receipt.attached', 'action:' || new.action_id, new.project,
                             data || jsonb_build_object('approver', new.auth_approver));
    end if;
    return new;
end $$;


create trigger ameesh_events_action_insert
    after insert on actions
    for each row execute function ameesh_events_action();
create trigger ameesh_events_action
    after update of state, auth_receipt on actions
    for each row
    when (old.state is distinct from new.state
          or old.auth_receipt is distinct from new.auth_receipt)
    execute function ameesh_events_action();

-- 6. approvals recorded by ameesh-approve (signed decision on an artifact)
create or replace function ameesh_events_approval() returns trigger
language plpgsql as $$
begin
    perform ameesh_event('approval.recorded', 'approval:' || new.id, null,
        jsonb_build_object('approval', new.id, 'approver', new.approver,
                           'decision', new.decision, 'action', new.action));
    return new;
end $$;

drop trigger if exists ameesh_events_approval on mesh_approvals;
create trigger ameesh_events_approval
    after insert on mesh_approvals
    for each row execute function ameesh_events_approval();
