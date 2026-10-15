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

-- #845 (revue N/P2 2328) : la fenêtre du relevé et sa couverture, écrites
-- dans la base AVANT le premier appel HTTP. `ci_guard_scan` est revendiquée
-- par un UPSERT conditionnel : deux `ameesh notify --once` concurrents (ou
-- deux hôtes) ne peuvent pas mener le même relevé et doubler le coût ; un
-- relevé tué en vol laisse la fenêtre expirer (`ci.GUARD_SCAN_S`).
-- `complete`/`reason` disent si le relevé a couvert toutes les exécutions non
-- conclues : un relevé incomplet ne publie RIEN (les relevés précédents
-- vieillissent, et `exploitation.CI_GUARD_FRESH_S` les clôt).
create table if not exists ci_guard_scan (
    repo        text not null primary key,
    started_at  timestamptz not null,
    finished_at timestamptz,
    -- appels GitHub réellement consommés (budget `ci.GUARD_SCAN_MAX_CALLS`)
    calls       integer not null default 0,
    -- exécutions non conclues listées, et parmi elles celles couvertes
    runs        integer not null default 0,
    covered     integer not null default 0,
    complete    boolean,
    -- raison d'un relevé incomplet : budget, truncated, jobs_truncated,
    -- too_many_runs, runner_truncated, failed ; NULL si complet
    reason      text
);

-- #845 (revue N/P2 2328) : le relevé d'une exécution non conclue, réutilisé
-- sans appel quand elle est restée `queued` (aucun job n'a commencé : sa
-- liste de jobs ne bouge plus). Une exécution `in_progress` est toujours
-- relue ; le cache ne sert jamais à publier un relevé partiel.
create table if not exists ci_guard_run (
    repo      text not null,
    run_id    bigint not null,
    attempt   integer not null default 1,
    status    text not null default '',
    queued    integer not null default 0,
    oldest_id bigint,
    oldest_at timestamptz,
    -- runners tenant un job `in_progress` de cette exécution : {nom: job}
    runners   jsonb not null default '{}'::jsonb,
    seen_at   timestamptz not null default now(),
    primary key (repo, run_id, attempt)
);
