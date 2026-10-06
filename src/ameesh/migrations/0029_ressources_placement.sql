-- SPDX-License-Identifier: AGPL-3.0-only
-- 0029_ressources_placement — ressources des hôtes, admission et visibilité
-- (lot L31, décisions 0028 et 0029).
--
-- 1. `host_resources` : relevés périodiques publiés par chaque exécuteur —
--    mémoire disponible, swap utilisé, charge 1 minute, disque libre du
--    dossier de travail, tours en cours. ÉTAT D'EXÉCUTION (base) : les seuils,
--    eux, sont déclaratifs (canon, `policy.resources` de la fiche `Host`).
--    `ameesh hosts` lit le dernier relevé de chaque hôte et l'historique court.
-- 2. `turn_resources` : rattachement des ressources d'un tour (groupe de
--    processus, étiquette de conteneur) pour repérer celles qui survivent à
--    leur tour. Aucune suppression automatique : l'exécuteur ne fait que
--    signaler (`orphan_resource`).
-- 3. `visibility_checks` : cache COURT de la règle de visibilité (0029) —
--    une persona ne tourne sur un hôte que si son responsable et les
--    administrateurs de l'hôte ont accès au dépôt de mémoire de la persona ;
--    revérifié à chaque ouverture de session et à chaque déplacement.
-- 4. `agent_registry.admission` : l'admission lue au canon (hôtes admis,
--    étiquettes, dépôt de mémoire) est recopiée en colonnes déclaratives ;
--    `host` reste l'état d'exécution (l'hôte où la session tourne
--    effectivement). `priority` sert à choisir les agents à mettre en pause
--    en cas de pression critique (jamais au milieu d'un tour).

-- 1. ---------------------------------------------------------------- relevés
create table if not exists host_resources (
    id                  bigserial primary key,
    host                text not null,
    sampled_at          timestamptz not null default now(),
    mem_available_bytes bigint,
    swap_used_bytes     bigint,
    load1               double precision,
    cpu_count           integer,
    disk_free_bytes     bigint,
    disk_path           text,
    turns_in_progress   integer
);
create index if not exists host_resources_host_time_idx
    on host_resources (host, sampled_at desc, id desc);
create index if not exists host_resources_time_idx
    on host_resources (sampled_at desc);

-- 2. ---------------------------------------------------------------- orphelins
create table if not exists turn_resources (
    id          bigserial primary key,
    turn_id     text not null unique,
    agent       text not null,
    host        text not null,
    pgid        integer,
    label       text,
    containers  text[],
    started_at  timestamptz not null default now(),
    ended_at    timestamptz,
    -- running : le tour a la ressource ; done : rendue proprement ;
    -- orphan : la ressource survit au tour (jamais supprimée ici)
    status      text not null default 'running'
        check (status in ('running', 'done', 'orphan'))
);
create index if not exists turn_resources_host_time_idx
    on turn_resources (host, started_at desc, id desc);
create index if not exists turn_resources_agent_time_idx
    on turn_resources (agent, started_at desc, id desc);

-- 3. ------------------------------------------------------------ visibilité
create table if not exists visibility_checks (
    persona     text not null,
    host        text not null,
    ok          boolean not null,
    diagnostic  text not null default '',
    repository  text,
    -- empreinte du contexte VÉRIFIÉ (dépôt+forge, humains requis, comptes,
    -- mode) : un verdict n'est réutilisé que pour un contexte identique.
    context     text not null default '',
    checked_at  timestamptz not null default now(),
    expires_at  timestamptz not null,
    primary key (persona, host)
);
create index if not exists visibility_checks_expiry_idx
    on visibility_checks (expires_at);

-- 4. --------------------------------------------------- registre des agents
-- Colonnes DÉCLARATIVES écrites par `canon sync` : elles décrivent
-- l'admission et la politique, jamais la position courante.
alter table agent_registry
    add column if not exists priority integer not null default 0,
    add column if not exists admitted_hosts text[],
    add column if not exists admitted_tags text[],
    add column if not exists memory_repository text,
    add column if not exists visibility_ok boolean,
    add column if not exists visibility_diagnostic text;

-- La vue d'observabilité fige ses colonnes (`r.*` est développé à la
-- création) : on la recrée, à l'identique de 0022, augmentée des colonnes de
-- L31. La condition de réclamation reste évaluée par `registry.overview`.
drop view if exists agent_mesh_overview;
create view agent_mesh_overview as
select
    r.*,
    extract(epoch from r.lease_expires_at)::float8 as lease_expires_ts,
    extract(epoch from r.last_seen)::float8        as last_seen_ts,
    extract(epoch from r.last_turn_at)::float8     as last_turn_ts,
    (r.public_key_fingerprint is not null and r.key_revoked_at is null) as key_ready,
    (r.public_key_fingerprint is not null and r.key_revoked_at is null
     and r.key_role = 'owner')                                        as has_owner_key,
    extract(epoch from r.ephemeral_expires_at)::float8                as ephemeral_expires_ts,
    (coalesce(r.responsible, '') <> ''
     and (not r.ephemeral or r.ephemeral_expires_at > now()))         as responsible_ok,
    (select count(*) from agent_mailbox m
      where m.recipient = r.name and m.delivered_at is null) as unread
from agent_registry r;
