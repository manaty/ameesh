-- SPDX-License-Identifier: AGPL-3.0-only
-- #845 : garde de la CI par l'API GitHub, sans SSH (verdict #828 du 13/10).
--
-- Deux métriques distinctes, deux alertes :
--   (a) `ci_runner_stuck` : un runner auto-hébergé `busy` sans aucun job
--       `in_progress` qui lui est attribué. L'API ne dit pas depuis quand ; le
--       relevé (`ci.Watcher`, `ameesh notify`) date le début de l'épisode dans
--       `ci_runner_state.stuck_since`, remis à NULL dès qu'il finit.
--   (b) `ci_queue_saturated` : l'âge du plus vieux job `queued` du dépôt
--       (toutes exécutions non conclues), au-delà du SLO de file (15 min,
--       canon §9). C'est un signal de capacité (#824), pas une panne — d'où la
--       distinction d'avec (a). La date est celle du job : un run reste
--       `queued` tant qu'un de ses jobs attend, une re-exécution garde le
--       `created_at` de son run, et un run `in_progress` peut encore porter un
--       job en file.
--
-- Chevauchement #747/#845 tranché par le pilote (13/10 11:02) : #747 garde
-- l'observation du temps d'attente des exécutions (colonnes, durées, `ci
-- status`) mais retire sa notification `ci_queue_starved`, qui faisait doublon
-- avec (b) ; (a) et (b) restent les deux seules clés d'alerte, une par cause
-- et par dépôt, et #845 porte seul le signal de capacité.
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
    -- identifiant GitHub du plus vieux job `queued` vu, et sa date de création
    oldest_id  bigint,
    oldest_at  timestamptz,
    -- nombre de jobs `queued` vus, toutes exécutions non conclues
    queued     integer not null default 0,
    seen_at    timestamptz not null default now()
);
