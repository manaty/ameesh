-- SPDX-License-Identifier: AGPL-3.0-only
-- 0045_menage — ménage de ce que les agents créent (lot L73, décision 0028
-- point 4, lot de base #1 « cycle de vie des worktrees »).
--
-- 1. `host_resources` : occupation du dossier temporaire du système (`/tmp`)
--    — chemin, type de système de fichiers, taille, octets utilisés. Un
--    tmpfs vit en mémoire (et en swap) : sa contre-pression compte (seuil
--    `max_tmpfs_used` de `policy.resources`).
-- 2. `managed_worktrees` : les worktrees git apparus PENDANT un tour d'agent
--    (diff de `git worktree list` avant/après), rattachés à l'agent, au tour
--    et au lot. Ils sont retirés à la fin du lot (fusion ou fermeture) s'ils
--    sont propres et que leur HEAD est sur le dépôt distant ; sinon gardés,
--    et signalés (`worktree_kept`).
-- 3. `housekeeping_log` : le journal du ménage — ce qui est supprimé, évincé,
--    retiré, gardé ou seulement signalé, avec sa taille. Hors des dossiers
--    gérés, rien n'est supprimé ; seule exception, les worktrees enregistrés
--    en (2), retirés par `git worktree remove` (jamais forcé).

-- 1. ---------------------------------------------------------------- /tmp
alter table host_resources
    add column if not exists tmp_path       text,
    add column if not exists tmp_fstype     text,
    add column if not exists tmp_size_bytes bigint,
    add column if not exists tmp_used_bytes bigint;

-- 2. ---------------------------------------------------------- worktrees
create table if not exists managed_worktrees (
    id          bigserial primary key,
    host        text not null,
    path        text not null,
    repo        text not null default '',
    agent       text not null,
    lot         text,
    turn_id     text,
    branch      text not null default '',
    head        text not null default '',
    created_at  timestamptz not null default now(),
    -- active : en vie, attend la fin de son lot ; removed : retiré par le
    -- ménage ; kept : fin de lot constatée mais travail non poussé (alerte) ;
    -- gone : dossier disparu hors ameesh (git worktree prune)
    status      text not null default 'active'
        check (status in ('active', 'removed', 'kept', 'gone')),
    detail      text not null default '',
    checked_at  timestamptz,
    ended_at    timestamptz
);
create unique index if not exists managed_worktrees_live_idx
    on managed_worktrees (host, path) where status in ('active', 'kept');
create index if not exists managed_worktrees_host_idx
    on managed_worktrees (host, status, created_at desc);

-- 3. ------------------------------------------------------------ journal
create table if not exists housekeeping_log (
    id      bigserial primary key,
    host    text not null,
    at      timestamptz not null default now(),
    actor   text not null default '',
    -- tmp (dossier temporaire d'un agent), cache (cache partagé), worktree,
    -- orphan (hors dossiers gérés : signalé seulement), mesure (bilan)
    kind    text not null,
    -- deleted, evicted, removed, kept, signaled, measured, planned (essai)
    action  text not null,
    path    text not null default '',
    bytes   bigint,
    agent   text,
    lot     text,
    detail  text not null default '',
    data    jsonb
);
create index if not exists housekeeping_log_host_time_idx
    on housekeeping_log (host, at desc, id desc);
