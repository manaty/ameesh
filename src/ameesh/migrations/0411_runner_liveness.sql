-- SPDX-License-Identifier: AGPL-3.0-only
-- #508 : preuve de vie du runner indépendante du bail et des hooks.
alter table agent_registry add column if not exists runner_seen_at timestamptz;
alter table agent_registry add column if not exists runner_seen_host text;
alter table agent_registry add column if not exists runner_seen_owner text;
