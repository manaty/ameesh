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
-- `mesh_keys` is what the key lookups read: the key holders, then the
-- agents' keys for the names no key holder has (a human's key is never
-- shadowed by an agent row created later under the same name).
--
-- The stopgap of 2026-10-12 09:49 (a registry row `smichea`, harness none,
-- mode externe, responsible human:smichea, no turn, made by SQL) is copied
-- here, with its key if one was registered meanwhile: its key is read from
-- here at once. The registry row itself is left (an additive migration
-- deletes nothing, L151); `key register --human smichea` removes it.

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

insert into key_holders (name, human, public_key, public_key_fingerprint, key_role,
                         key_updated_at, key_revoked_at, created_at)
select r.name, r.responsible, r.public_key, r.public_key_fingerprint, r.key_role,
       r.key_updated_at, r.key_revoked_at, r.created_at
  from agent_registry r
 where r.harness = 'none' and r.mode = 'externe' and r.turns = 0
   and r.responsible = 'human:' || r.name
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
