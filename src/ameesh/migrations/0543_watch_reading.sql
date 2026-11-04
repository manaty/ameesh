-- SPDX-License-Identifier: AGPL-3.0-only
-- 0543_watch_reading — #5698 : l'empreinte de la dernière lecture de veille.
--
-- Un tir de `ameesh watch send` fait d'abord sa propre lecture (projets,
-- alertes actives, lots dus de l'agent) et la condense en une empreinte
-- stable. La comparer à `last_reading` évite d'ouvrir un tour quand rien n'a
-- changé : le tir suivant dépose alors une note passive `--info` (lue au tour
-- suivant), ou rien si l'agent a eu un tour dans les 30 dernières minutes.
--
-- Additive seulement : une colonne nullable, aucune donnée réécrite. Le
-- premier tir après cette migration la trouve NULL et envoie l'invite une
-- fois, comme avant.

alter table agent_watches add column if not exists last_reading text;

comment on column agent_watches.last_reading is
    '#5698: sha256 hex of the watch''s last reading (projects, active alerts, due lots); NULL = never read, the next firing sends the instruction.';
