-- SPDX-License-Identifier: AGPL-3.0-only
-- 0054_ci_test_results — résultats de CI relevés en direct (L161c, étude L161).
--
-- `ameesh notify`, sur l'hôte désigné (`github.host`), relève pendant les
-- tests le check run « tests (flux) » de chaque exécution (scripts/ci-flux.py
-- publier) et remonte chaque échec au courrier de l'agent du lot. Ces deux
-- tables en sont la mémoire : une ligne par exécution suivie, une par test en
-- échec, jamais une par réussite. Elles servent à ne pas envoyer deux fois, au
-- rouge hérité (le test échouait déjà sur la base), au retrait des messages à
-- la poussée suivante, à `ameesh ci status` et à l'alerte `ci_main_red`.

create table if not exists ci_runs (
    repo               text not null,
    run_id             bigint not null,
    head_sha           text not null,
    branch             text not null,
    event              text not null default '',
    -- référence du rouge hérité : la cible de la PR, sinon main
    base               text not null default 'main',
    work_item_id       bigint,
    -- running → success | failure | cancelled ; superseded : une poussée plus
    -- récente de la même branche l'a remplacée
    status             text not null default 'running'
                       check (status in ('running', 'success', 'failure', 'cancelled',
                                         'superseded')),
    superseded_by      text,
    url                text,
    first_seen_at      timestamptz not null default now(),
    concluded_at       timestamptz,
    urgent_message_id  bigint,
    verdict_message_id bigint,
    primary key (repo, run_id)
);
create index if not exists ci_runs_branch_idx on ci_runs (repo, branch, first_seen_at desc);
create index if not exists ci_runs_lot_idx on ci_runs (work_item_id, first_seen_at desc)
    where work_item_id is not null;

create table if not exists ci_test_failures (
    repo        text not null,
    run_id      bigint not null,
    part        text not null default '',
    test        text not null,
    module      text not null default '',
    status      text not null default 'echec',
    -- le test échouait déjà au dernier passage connu de la base (main)
    inherited   boolean not null default false,
    excerpt     text not null default '',
    replay      text not null default '',
    duration_s  double precision,
    failed_at   timestamptz,
    message_id  bigint,
    recorded_at timestamptz not null default now(),
    primary key (repo, run_id, part, test),
    foreign key (repo, run_id) references ci_runs (repo, run_id) on delete cascade
);
