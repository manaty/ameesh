-- SPDX-License-Identifier: AGPL-3.0-only
-- 0052_work_item_estimates — durée estimée de chaque lot, durée réelle
-- mesurée (lot L157).
--
-- Demande du propriétaire (2026-10-11) : « ameesh orchestre des harnais et
-- doit prévoir la roadmap complète des projets ; il doit toujours estimer le
-- temps que prennent les tâches, dès la conception des lots ; ensuite
-- l'auditeur audite le processus d'estimation et l'ajuste ».
--
-- 1. Durée ESTIMÉE d'un lot (`work_items.estimate_*`) : en minutes, avec sa
--    source (« conception », « historique bug ×1,3 »…), son auteur et son
--    instant. Posée à la création (`ameesh work add --estimate 2h`, `mail
--    send --new-lot … --estimate 2h`) ou ensuite (`ameesh work plan <id>
--    --estimate 90m`). Ces colonnes portent l'estimation EN VIGUEUR.
--
-- 2. `work_item_estimates` : l'historique des estimations d'un lot, une ligne
--    par estimation posée. La comparaison au réel (`ameesh work estimates`)
--    prend l'estimation en vigueur au DÉBUT du travail : une ré-estimation en
--    cours de route ne masque jamais l'écart (elle reste visible, comptée à
--    part par l'auditeur).
--
-- 3. `work_items.started_at` : début MESURÉ du travail — le premier passage
--    en `build` (ou en `qa`), ou le premier tour de l'agent assigné sur le
--    lot (noté par l'exécuteur), le premier des deux. La fin est le jalon
--    `merged` (0013, automatique). Durée réelle = fusion − début.
--
-- Reprise : le début des lots existants est relu dans le journal (premier
-- passage en build ou qa) et dans les délégations (premier tour du délégué).

alter table work_items
    add column if not exists estimate_minutes integer,
    add column if not exists estimate_source  text,
    add column if not exists estimate_by      text,
    add column if not exists estimate_at      timestamptz,
    add column if not exists started_at       timestamptz;

alter table work_items drop constraint if exists work_items_estimate_minutes_check;
alter table work_items add constraint work_items_estimate_minutes_check
    check (estimate_minutes is null or estimate_minutes > 0);

create table if not exists work_item_estimates (
    id           bigserial primary key,
    work_item_id bigint not null references work_items(id) on delete cascade,
    minutes      integer not null check (minutes > 0),
    source       text,
    estimated_by text not null default '',
    at           timestamptz not null default now()
);

create index if not exists work_item_estimates_item_idx
    on work_item_estimates (work_item_id, at, id);

comment on table work_item_estimates is
  'L157 : historique des durées estimées d''un lot (minutes, source, auteur) ; la comparaison au réel prend celle en vigueur au début du travail.';

update work_items w
   set started_at = s.at
  from (select x.work_item_id, min(x.at) as at
          from (select e.work_item_id, e.created_at as at
                  from work_item_events e
                 where e.state in ('build', 'qa')
                union all
                select d.work_item_id, d.first_turn_at
                  from work_item_delegations d
                 where d.first_turn_at is not null) x
         group by x.work_item_id) s
 where s.work_item_id = w.id and w.started_at is null;
