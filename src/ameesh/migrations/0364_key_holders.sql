-- SPDX-License-Identifier: AGPL-3.0-only
-- 0364_key_holders — a human's public key outside the agent registry (#330).
--
-- `key register` only wrote a key onto an `agent_registry` row: the owner
-- (a human, `human:smichea`) could not register theirs, and a registry row
-- for them would be an agent — claimable by `attach`, overwritten by a hook
-- session or a canon card of that name, listed in projects, raising mail
-- alerts. A key holder lives here instead: a name, the human it is, a key.
-- Never executable, never woken, in no project, alert or L165 take: nothing
-- but the key lookups reads this table.
--
-- Who may write it (#326 r1). Every agent shares the executor's database
-- role, so the rules are the table's own, in a trigger the executor can
-- neither drop nor disable (no TRIGGER right, no ownership:
-- deploy/sql/role-executor.sql audits both):
--   * a first key: a new holder, and for an `owner` key only while no owner
--     key exists at all (revoked ones included); a unique index keeps it to
--     one owner key holder, two concurrent transactions included;
--   * a revocation: `key_revoked_at` set, nothing else changed;
--   * anything else — replacing or rotating a key, a key after a
--     revocation, removing a holder — is the owner's own gesture: a role
--     that is a member of this table's owner (the migration owner, never the
--     executor), with `ameesh.owner_gesture` set to `on` in the transaction
--     (`ameesh key register --human … --replace`, run with that role's DSN).
--     The setting alone proves nothing: the role is checked.
-- The same holds for an owner key on an agent's row (#326 r2: the owner's
-- authority delegated to an agent, `key register <agent> --role owner`):
-- set, changed, moved to another name or brought back from a revocation by
-- the owner's gesture only. No agent may be created under a holder's name.
-- Dates are the server's (`now()`), never the writer's; every key event goes
-- to `key_events`, append-only and written by these triggers only, which
-- `ameesh alerts` reads (`owner_key_registered`): a revocation right after
-- use does not erase it.
--
-- `mesh_keys` is what the key lookups read: the key holders, then the
-- agents' keys for the names no key holder has.
--
-- The stopgap of 2026-10-12 09:49 — exactly the registry row `smichea`,
-- harness none, mode externe, responsible human:smichea, no turn, session,
-- lease or canon card, made by SQL — is copied here if a key was registered
-- on it meanwhile. The row itself is left (an additive migration removes
-- nothing, L151); `key register --human smichea` takes it out.

create table if not exists key_holders (
    name                   text primary key
                           check (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    human                  text not null check (human ~ '^human:.+'),
    public_key             text,
    public_key_fingerprint text,
    key_role               text not null default 'agent'
                           check (key_role in ('owner', 'agent')),
    key_updated_at         timestamptz,
    key_revoked_at         timestamptz,
    created_at             timestamptz not null default now()
);

-- one owner key holder at a time, revoked or not (#326 r2): two first owner
-- keys inserted at once by two transactions cannot both commit, whatever
-- their isolation level
create unique index if not exists key_holders_one_owner
    on key_holders (key_role) where key_role = 'owner';

-- every key event, append-only (#326 r2): written by the triggers below
-- only, never updated nor deleted — a revocation right after use leaves the
-- registration here, and `owner_key_registered` reads it
create table if not exists key_events (
    id            bigserial primary key,
    at            timestamptz not null default now(),
    name          text not null,
    holder        text not null check (holder in ('human', 'agent')),
    event         text not null
                  check (event in ('registered', 'replaced', 'revoked', 'removed')),
    key_role      text,
    fingerprint   text,
    db_user       text not null default current_user,
    owner_gesture boolean not null default false
);
create index if not exists key_events_at on key_events (at);

create or replace function key_holders_owner_gesture() returns boolean
language sql stable as $$
    select coalesce(current_setting('ameesh.owner_gesture', true), '') = 'on'
       and pg_has_role(current_user,
                       (select relowner from pg_class where oid = 'key_holders'::regclass),
                       'MEMBER')
$$;

create or replace function key_events_guard() returns trigger
language plpgsql as $$
begin
    if tg_op <> 'INSERT' then
        raise exception 'key_events is append-only: an event is never changed nor removed'
            using errcode = 'insufficient_privilege';
    end if;
    if pg_trigger_depth() < 2 then
        raise exception 'key_events is written by the key triggers only'
            using errcode = 'insufficient_privilege';
    end if;
    new.at := now();
    new.db_user := current_user;
    return new;
end
$$;

drop trigger if exists key_events_guard on key_events;
create trigger key_events_guard
    before insert or update or delete on key_events
    for each row execute function key_events_guard();

-- the event a key row's change is, or null
create or replace function key_event_of(op text, o_key text, o_fp text, o_role text,
                                        o_revoked timestamptz, n_key text, n_fp text,
                                        n_role text, n_revoked timestamptz) returns text
language sql immutable as $$
    select case
        when op = 'DELETE' then case when o_key is not null then 'removed' end
        when n_key is null then null
        when op = 'INSERT' then case when n_revoked is null then 'registered' end
        when o_key is distinct from n_key or o_fp is distinct from n_fp
             or o_role is distinct from n_role
             or (o_revoked is not null and n_revoked is null)
            then case when o_key is null then 'registered' else 'replaced' end
        when o_revoked is null and n_revoked is not null then 'revoked'
    end
$$;

create or replace function key_events_log() returns trigger
language plpgsql as $$
declare
    ev text;
    r  record;
begin
    if tg_op = 'DELETE' then r := old; else r := new; end if;
    if tg_op = 'INSERT' then
        ev := key_event_of(tg_op, null, null, null, null, new.public_key,
                           new.public_key_fingerprint, new.key_role, new.key_revoked_at);
    elsif tg_op = 'DELETE' then
        ev := key_event_of(tg_op, old.public_key, old.public_key_fingerprint, old.key_role,
                           old.key_revoked_at, null, null, null, null);
    else
        ev := key_event_of(tg_op, old.public_key, old.public_key_fingerprint, old.key_role,
                           old.key_revoked_at, new.public_key, new.public_key_fingerprint,
                           new.key_role, new.key_revoked_at);
        if ev is null and old.name <> new.name and new.public_key is not null then
            ev := 'registered';         -- a key moved to another name
        end if;
    end if;
    if ev is not null then
        insert into key_events (name, holder, event, key_role, fingerprint, owner_gesture)
        values (r.name, case when tg_table_name = 'key_holders' then 'human' else 'agent' end,
                ev, r.key_role, r.public_key_fingerprint, key_holders_owner_gesture());
    end if;
    return null;
end
$$;

create or replace function key_holders_guard() returns trigger
language plpgsql as $$
begin
    if key_holders_owner_gesture() then
        return coalesce(new, old);
    end if;
    if tg_op = 'INSERT' then
        if new.key_role = 'owner' and exists (
                select 1 from key_holders where key_role = 'owner') then
            raise exception 'an owner key already exists: replacing it is the owner''s own gesture (key register --human % --replace, with the owner''s database role)', new.name
                using errcode = 'insufficient_privilege';
        end if;
        if new.key_revoked_at is not null then
            raise exception 'a key holder is not created revoked'
                using errcode = 'insufficient_privilege';
        end if;
        new.key_updated_at := now();    -- the server's clock, never the writer's
        new.created_at := now();
        return new;
    end if;
    if tg_op = 'UPDATE' and old.key_revoked_at is null and new.key_revoked_at is not null
            and new.name = old.name and new.human = old.human
            and new.public_key is not distinct from old.public_key
            and new.public_key_fingerprint is not distinct from old.public_key_fingerprint
            and new.key_role = old.key_role
            and new.key_updated_at is not distinct from old.key_updated_at
            and new.created_at = old.created_at then
        new.key_revoked_at := now();
        return new;                     -- a revocation
    end if;
    raise exception 'the key of % is only replaced, rotated or removed by the owner''s own gesture (key register --human % --replace, with the owner''s database role)', old.name, old.name
        using errcode = 'insufficient_privilege';
end
$$;

drop trigger if exists key_holders_guard on key_holders;
create trigger key_holders_guard
    before insert or update or delete on key_holders
    for each row execute function key_holders_guard();

drop trigger if exists key_holders_events on key_holders;
create trigger key_holders_events
    after insert or update or delete on key_holders
    for each row execute function key_events_log();

create or replace function agent_registry_not_a_key_holder() returns trigger
language plpgsql as $$
begin
    if exists (select 1 from key_holders where name = new.name) then
        raise exception '% holds a human''s key: no agent is created under that name', new.name
            using errcode = 'unique_violation';
    end if;
    return new;
end
$$;

drop trigger if exists agent_registry_not_a_key_holder on agent_registry;
create trigger agent_registry_not_a_key_holder
    before insert on agent_registry
    for each row execute function agent_registry_not_a_key_holder();

-- an agent's key (#326 r2): an owner key — the owner's authority delegated
-- to an agent — is set, changed, moved to another name or brought back
-- from a revocation by the owner's own gesture only; an agent key and a
-- revocation stay free. The key's date is the server's.
create or replace function agent_registry_key_guard() returns trigger
language plpgsql as $$
declare
    changed boolean;
begin
    if tg_op = 'INSERT' then
        changed := new.public_key is not null;
    else
        changed := new.public_key is distinct from old.public_key
                or new.public_key_fingerprint is distinct from old.public_key_fingerprint
                or new.key_role is distinct from old.key_role
                or (old.key_revoked_at is not null and new.key_revoked_at is null);
    end if;
    if key_holders_owner_gesture() then
        return new;
    end if;
    if new.key_role = 'owner' and new.public_key is not null and new.key_revoked_at is null
            and (changed or (tg_op = 'UPDATE' and new.name <> old.name)) then
        raise exception 'an owner key on agent % is the owner''s own gesture (key register % --role owner, with the owner''s database role)', new.name, new.name
            using errcode = 'insufficient_privilege';
    end if;
    if changed then
        new.key_updated_at := now();
    elsif tg_op = 'UPDATE' then
        new.key_updated_at := old.key_updated_at;
    end if;
    if tg_op = 'UPDATE' and new.key_revoked_at is distinct from old.key_revoked_at
            and new.key_revoked_at is not null then
        new.key_revoked_at := now();
    end if;
    return new;
end
$$;

drop trigger if exists agent_registry_key_guard on agent_registry;
create trigger agent_registry_key_guard
    before insert or update of public_key, public_key_fingerprint, key_role, key_revoked_at,
                               key_updated_at, name on agent_registry
    for each row execute function agent_registry_key_guard();

drop trigger if exists agent_registry_key_events on agent_registry;
create trigger agent_registry_key_events
    after insert or delete or update of public_key, public_key_fingerprint, key_role,
                                        key_revoked_at, name on agent_registry
    for each row execute function key_events_log();

insert into key_holders (name, human, public_key, public_key_fingerprint, key_role,
                         key_updated_at, created_at)
select r.name, r.responsible, r.public_key, r.public_key_fingerprint, r.key_role,
       r.key_updated_at, r.created_at
  from agent_registry r
 where r.name = 'smichea' and r.responsible = 'human:smichea'
   and r.harness = 'none' and r.mode = 'externe' and r.turns = 0
   and r.session_id is null and r.lease_owner is null and r.canon_ref is null
   and r.public_key is not null and r.key_revoked_at is null
   and (r.key_role <> 'owner'
        or not exists (select 1 from key_holders where key_role = 'owner'))
on conflict (name) do nothing;

create or replace view mesh_keys as
select h.name, 'human' as holder, h.public_key, h.public_key_fingerprint, h.key_role,
       h.key_updated_at, h.key_revoked_at
  from key_holders h
union all
select r.name, 'agent', r.public_key, r.public_key_fingerprint, r.key_role,
       r.key_updated_at, r.key_revoked_at
  from agent_registry r
 where not exists (select 1 from key_holders h where h.name = r.name);

-- approvals are checked against the same keys (columns unchanged)
create or replace view mesh_approvals_status as
select
    a.*,
    (a.consumed_at is null)                    as unconsumed,
    (a.expires_at > now())                     as unexpired,
    (r.public_key is not null)                 as key_known,
    (r.key_revoked_at is null)                 as key_live,
    (r.public_key_fingerprint = a.signature_key) as key_matches,
    extract(epoch from a.expires_at)::float8   as expires_ts,
    (r.key_role = 'owner')                     as key_is_owner
from mesh_approvals a
left join mesh_keys r on r.name = a.approver;
