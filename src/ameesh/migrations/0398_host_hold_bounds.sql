-- SPDX-License-Identifier: AGPL-3.0-only
-- 0398_host_hold_bounds — host holds bounded on database time, with their
-- history (lot #254, codex r1).
--
-- 0392 left the 24 h bound to the CLI's clock and kept no history: a
-- replacement overwrote the hold, a release deleted it. Here the table
-- bounds every write itself (after the moment it is set, 24 h at most, on
-- database time), and every hold and release lands in `host_hold_events`,
-- in the same transaction as the change (the project thread keeps only a
-- readable copy). A hold that ends by expiry leaves no event: it is read
-- past, never deleted. Repeatable, and upgrades a base that applied 0392.

-- a hold already outside the bound (none expected: the CLI capped it) ends:
-- its end is cut to the bound, or to one second after it was set; the row
-- itself stays (an expired hold leaves no event either, it is read past)
update host_holds set until = least(until, created_at + interval '24 hours')
 where until > created_at + interval '24 hours';
update host_holds set until = created_at + interval '1 second'
 where until <= created_at;

do $$
begin
    if not exists (select 1 from pg_constraint
                    where conname = 'host_holds_until_bounded'
                      and conrelid = 'host_holds'::regclass) then
        alter table host_holds add constraint host_holds_until_bounded
            check (until > created_at and until <= created_at + interval '24 hours');
    end if;
end $$;

create table if not exists host_hold_events (
    id         bigserial primary key,
    host       text not null check (host <> ''),
    action     text not null check (action in ('hold', 'release')),
    author     text not null check (author <> ''),
    reason     text,
    until      timestamptz,
    at         timestamptz not null default now(),
    check ((action = 'hold') = (reason is not null and until is not null))
);
create index if not exists host_hold_events_host on host_hold_events (host, at);
