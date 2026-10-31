-- SPDX-License-Identifier: AGPL-3.0-only
-- #4995: the signed replacement log and its marker resist a SQL writer.
--
-- #4991 recorded each signed replacement in `decision_supersessions` and
-- cached it in `payload.decision.superseded_by`; both writes happen in one
-- decision transaction. Nothing stopped an executor role holding the
-- default table DML (role-executor.sql grants it on every new table) from
-- rewriting or removing those rows, and the authority check read only the
-- mutable marker.
--
-- Minimal rights: the log keeps SELECT (diagnostics) and INSERT (the decision
-- cycle appends one event; a delegated answer may run under the executor
-- role), never UPDATE nor DELETE. The table owner and a superuser remain able
-- to repair it with DDL/DML: this guard protects against an executor writer,
-- not against the owner (as #4995 scopes it). The append-only trigger then
-- refuses an *accidental* UPDATE/DELETE grant, and the authority read proves
-- the signed replacement: a forged INSERT can only deny the old authority
-- (fail closed until the owner repairs it), never make a replacement
-- effective.
set local lock_timeout = '5s';

do $supersession_acl$
declare item record;
begin
    -- Table-level grants (including the executor's default DML): keep the
    -- append path (SELECT, INSERT); remove every rewrite/erase right.
    for item in
        select distinct c.relname, acl.grantee, acl.privilege_type
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
          cross join lateral aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) acl
         where n.nspname = current_schema()
           and c.relname = 'decision_supersessions'
           and acl.grantee <> c.relowner
           and acl.privilege_type not in ('SELECT', 'INSERT')
    loop
        execute format('REVOKE %s ON %I.%I FROM %s', item.privilege_type,
                       current_schema(), item.relname,
                       case when item.grantee = 0 then 'PUBLIC'
                            else quote_ident(pg_get_userbyid(item.grantee)) end);
    end loop;
    -- Column grants survive a table-level REVOKE; remove them too (INSERT is
    -- granted at table level, a column-only INSERT would not be the contract).
    for item in
        select distinct a.attname, acl.grantee, acl.privilege_type
          from pg_attribute a
          join pg_class c on c.oid = a.attrelid
          join pg_namespace n on n.oid = c.relnamespace
          cross join lateral aclexplode(a.attacl) acl
         where n.nspname = current_schema()
           and c.relname = 'decision_supersessions'
           and a.attnum > 0 and not a.attisdropped
           and acl.grantee <> c.relowner
    loop
        execute format('REVOKE %s (%I) ON %I.%I FROM %s', item.privilege_type,
                       item.attname, current_schema(), 'decision_supersessions',
                       case when item.grantee = 0 then 'PUBLIC'
                            else quote_ident(pg_get_userbyid(item.grantee)) end);
    end loop;
end
$supersession_acl$;

-- Enrolled release controllers re-read the signed authority at every step
-- (autorite-release.py -> decisions.require_current) and need SELECT on this
-- log. role-release-controller.sql grants it on (re)enrollment; controllers
-- already enrolled before this migration would otherwise fail every signed
-- operation with a permission error, so grant it here without a manual step.
-- The name prefix is the enrollment convention (default
-- ameesh_release_controller, then one group per host); a controller enrolled
-- under another name is covered by reapplying its role script.
do $supersession_controller$
declare controller record;
begin
    for controller in
        select rolname from pg_roles
         where rolname = 'ameesh_release_controller'
            or rolname like 'ameesh_release_controller\_%'
    loop
        execute format('GRANT SELECT ON %I.decision_supersessions TO %I',
                       current_schema(), controller.rolname);
    end loop;
end
$supersession_controller$;

-- A signed replacement event is never rewritten nor removed by a writer:
-- the owner alone keeps the repair path (it can still drop this trigger).
create function ameesh_decision_supersessions_append_only() returns trigger
language plpgsql as $$
begin
    if current_user::regrole = (select relowner from pg_class where oid = TG_RELID) then
        if TG_OP = 'DELETE' then
            return OLD;
        end if;
        return NEW;
    end if;
    raise exception 'decision_supersessions is append-only: a signed replacement event is never rewritten';
end $$;

create trigger ameesh_decision_supersessions_append_only
    before update or delete on decision_supersessions
    for each row execute function ameesh_decision_supersessions_append_only();

-- The denormalised marker is append-only once set: a writer that can update
-- mail rows may set it only together with the logged replacement (visible in
-- the same transaction; the decision cycle inserts that row first), and may
-- never clear or move it. The owner keeps the repair path, and the authority
-- read (decisions.require_current) verifies the signed event instead of
-- trusting this cache.
create function ameesh_decision_supersession_marker_guard() returns trigger
language plpgsql as $$
declare
    before_marker bigint := nullif(OLD.payload->'decision'->>'superseded_by', '')::bigint;
    after_marker bigint := nullif(NEW.payload->'decision'->>'superseded_by', '')::bigint;
begin
    if before_marker is not null then
        -- A marker once set is never moved nor cleared by a writer; only the
        -- table owner keeps the repair path.
        if current_user::regrole = (select relowner from pg_class where oid = TG_RELID) then
            return NEW;
        end if;
        raise exception 'decision supersession marker is append-only';
    end if;
    if after_marker is null then
        return NEW;
    end if;
    if not exists (select 1 from decision_supersessions
                    where supersedes = OLD.id and superseded_by = after_marker) then
        raise exception 'decision supersession marker requires its logged signed replacement';
    end if;
    return NEW;
end $$;

create trigger ameesh_decision_supersession_marker_guard
    before update on agent_mailbox
    for each row
    when (OLD.payload->'decision'->>'superseded_by'
          is distinct from NEW.payload->'decision'->>'superseded_by')
    execute function ameesh_decision_supersession_marker_guard();
