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
--     key exists at all (revoked ones included) — `ameesh alerts` then
--     raises `owner_key_registered` and the owner's chat gets a note;
--   * a revocation: `key_revoked_at` set, nothing else changed;
--   * anything else — replacing or rotating a key, a key after a
--     revocation, a second owner key, removing a holder — is the owner's own
--     gesture: a role that is a member of this table's owner (the migration
--     owner, never the executor), with `ameesh.owner_gesture` set to `on`
--     in the transaction (`ameesh key register --human … --replace`, run
--     with that role's DSN). The setting alone proves nothing: the role is
--     checked.
-- No agent may then be created under a holder's name (trigger on
-- agent_registry): a holder's rotation is never blocked by a later agent.
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

create or replace function key_holders_owner_gesture() returns boolean
language sql stable as $$
    select coalesce(current_setting('ameesh.owner_gesture', true), '') = 'on'
       and pg_has_role(current_user,
                       (select relowner from pg_class where oid = 'key_holders'::regclass),
                       'MEMBER')
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
        return new;
    end if;
    if tg_op = 'UPDATE' and old.key_revoked_at is null and new.key_revoked_at is not null
            and new.name = old.name and new.human = old.human
            and new.public_key is not distinct from old.public_key
            and new.public_key_fingerprint is not distinct from old.public_key_fingerprint
            and new.key_role = old.key_role
            and new.key_updated_at is not distinct from old.key_updated_at
            and new.created_at = old.created_at then
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
