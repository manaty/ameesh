-- SPDX-License-Identifier: AGPL-3.0-only
-- 0388_agent_projects — the projects an agent works on (L243-1, lot #435).
--
-- An Agent card may list several projects (`projects: [ameesh, nexlink]`, the
-- first being the primary one). `canon sync` writes them here, and keeps
-- writing `team` as the first of them: a reader of a single project reads
-- the primary one, unchanged. NULL for a row no card declares: its projects
-- are then `[team]`, else `[chantier]`; an empty list for a card that
-- declares none.

alter table agent_registry add column if not exists projects text[];
