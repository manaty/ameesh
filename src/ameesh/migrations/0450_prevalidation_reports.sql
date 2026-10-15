-- SPDX-License-Identifier: AGPL-3.0-only
-- 0450_prevalidation_reports — rapports de prévalidation au gel (#1770, L161e/L161f).
--
-- La prévalidation d'une tête candidate (sélection par impact de la carte
-- L161e et du plan L161f, exécutée sur une VM agents par `ameesh offload run
-- --fallback none`) rend un rapport `ameesh-prevalidation/1`. Il est écrit
-- APRÈS le run distant, hors de toute transaction : aucun verrou de base
-- n'est tenu pendant que la VM exécute la sélection.
--
-- Clé (work_item_id, head_sha) : une tête candidate a un seul rapport courant ;
-- rejouer la même tête remplace le précédent. `outcome`, `ok` et `causes` sont
-- recopiés du rapport complet pour une lecture courte (accroche du gel) ;
-- `report` garde le rapport entier (modules, parts, tests, hôte, durée).
--
-- Le rapport n'est pas un verdict : il ne remplace ni la désignation au gel
-- (#1617) ni le gel dérivé (#1756), et son écriture n'active aucune règle
-- (#1770 : le bloc `prevalidation` du canon n'est pas encore publié).

create table if not exists prevalidation_reports (
    work_item_id bigint not null,
    head_sha     text   not null,
    base_sha     text   not null default '',
    card_commit  text   not null default '',
    schema       text   not null default 'ameesh-prevalidation/1',
    outcome      text   not null,
    ok           boolean not null,
    causes       jsonb  not null default '[]'::jsonb
                 check (jsonb_typeof(causes) = 'array'),
    report       jsonb  not null
                 check (jsonb_typeof(report) = 'object'),
    recorded_at  timestamptz not null default now(),
    primary key (work_item_id, head_sha)
);
create index if not exists prevalidation_reports_lot_idx
    on prevalidation_reports (work_item_id, recorded_at desc);
