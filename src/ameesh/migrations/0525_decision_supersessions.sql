-- SPDX-License-Identifier: AGPL-3.0-only
-- #4991: immutable replacement events, also for decisions predating routing.
set local lock_timeout = '5s';
create table decision_supersessions (
    supersedes bigint primary key references agent_mailbox(id),
    superseded_by bigint not null unique references agent_mailbox(id),
    person text not null,
    occurred_at timestamptz not null,
    proof jsonb not null,
    check (supersedes < superseded_by)
);
