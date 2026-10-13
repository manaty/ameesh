-- SPDX-License-Identifier: AGPL-3.0-only
-- #845 : garde de la CI par l'API GitHub, sans SSH (verdict #828 du 13/10).
--
-- Deux métriques distinctes, deux alertes :
--   (a) `ci_runner_stuck` : un runner auto-hébergé `busy` sans aucun job
--       `in_progress` qui lui est attribué. L'API ne dit pas depuis quand ; le
--       relevé (`ci.Watcher`, `ameesh notify`) date le début de l'épisode dans
--       `ci_runner_state.stuck_since`, remis à NULL dès qu'il finit.
--   (b) `ci_queue_saturated` : l'âge du plus vieux job `queued` du dépôt,
--       au-delà du SLO de file (15 min, canon §9). C'est un signal de
--       capacité (#824), pas une panne — d'où la distinction d'avec (a).
--
-- `ci_queue_starved` (#747) reste l'alerte symétrique : une exécution attend
-- un runner alors que les runners sont idle. Ici les runners sont occupés (a)
-- ou la file est simplement longue (b) : trois clés d'alerte distinctes.
set local lock_timeout = '5s';

create table if not exists ci_runner_state (
    repo        text not null,
    runner_id   bigint not null,
    name        text not null,
    busy        boolean not null default false,
    -- job `in_progress` attribué à ce runner, si l'API en montre un
    job         text,
    -- depuis quand le runner est busy (diagnostic) ; NULL s'il est idle
    busy_since  timestamptz,
    -- depuis quand busy SANS job in_progress (l'épisode de panne) ; NULL sinon
    stuck_since timestamptz,
    seen_at     timestamptz not null default now(),
    primary key (repo, runner_id)
);
create index if not exists ci_runner_state_stuck_idx
    on ci_runner_state (repo, stuck_since) where stuck_since is not null;

create table if not exists ci_queue_state (
    repo       text not null primary key,
    -- plus vieux run `queued` vu (le job le plus ancien de la file)
    oldest_id  bigint,
    oldest_at  timestamptz,
    queued     integer not null default 0,
    seen_at    timestamptz not null default now()
);
