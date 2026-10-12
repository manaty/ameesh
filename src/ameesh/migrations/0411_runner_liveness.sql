-- SPDX-License-Identifier: AGPL-3.0-only
-- #508 : preuve de vie du runner indépendante du bail et des hooks.
-- Une attente de verrou échoue en 5 s ; la migration sera rejouée au déploiement suivant.
set local lock_timeout = '5s';
alter table agent_registry add column if not exists runner_seen_at timestamptz;
alter table agent_registry add column if not exists runner_seen_host text;
alter table agent_registry add column if not exists runner_seen_owner text;
