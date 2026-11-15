-- SPDX-License-Identifier: AGPL-3.0-only
-- #7441: scheduled waves and withheld switches share the append-only journal.
-- A planned row is a proposal, never proof of a running session's account.
-- ameesh: constraint not tightened
alter table account_switches drop constraint if exists account_switches_kind_check;
alter table account_switches add constraint account_switches_kind_check
    check (kind in ('bascule', 'retour', 'manuel', 'auto', 'config', 'agent',
                    'planned', 'hysteresis'));
create index if not exists account_switches_persona_idx
    on account_switches(harness, agent, id desc);
alter table account_switches add column if not exists wave_id text;
alter table account_switches add column if not exists wave_epoch bigint;
