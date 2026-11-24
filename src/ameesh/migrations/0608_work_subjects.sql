-- SPDX-License-Identifier: AGPL-3.0-only
-- 0608_work_subjects — #8193 (tranche 3 de #8191, issue manaty/ameesh#6403).
--
-- Un sujet groupe des lots d'un projet (« pricing-v2 ») : une ligne créée par
-- `ameesh work subject add`, identifiée par un slug court unique par projet,
-- avec titre, auteur, date et état ouvert/clos. `github_ref` est prête pour
-- la tranche 4 (épique GitHub) ; rien ne l'écrit encore.
--
-- Le lot porte sa référence de sujet dans `work_items.subject_id`, distincte
-- de `workstream` : celle-ci sert de repli de projet dans la garde des modules
-- (0363) et n'est pas surchargée. Pas de clé étrangère sur `work_items` : la
-- colonne reste une simple colonne nullable (additive), et le code vérifie le
-- sujet avant de l'écrire.
--
-- Additive et compatible N-1 : une table neuve et une colonne nullable que
-- l'ancien code ne lit ni n'écrit.

create table if not exists work_subjects (
    id         bigserial primary key,
    project    text not null,
    slug       text not null,
    title      text not null,
    author     text not null default '',
    state      text not null default 'open',
    github_ref text,
    created_at timestamptz not null default now(),
    closed_at  timestamptz,
    closed_by  text,
    constraint work_subjects_project_slug unique (project, slug),
    constraint work_subjects_slug_valid
        check (slug ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$' and length(slug) <= 63),
    constraint work_subjects_title_valid check (btrim(title) <> ''),
    constraint work_subjects_state_valid check (state in ('open', 'closed'))
);

alter table work_items add column if not exists subject_id bigint;

create index if not exists work_items_subject
    on work_items (subject_id) where subject_id is not null;
