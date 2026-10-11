-- SPDX-License-Identifier: AGPL-3.0-only
-- 0054_estimate_reference — table de référence des durées, publiée par
-- l'auditeur et seulement LUE par ameesh (lot L157b).
--
-- Constat du propriétaire (2026-10-11) : « tu estimes 10 fois trop les tâches
-- d'agents ; comment le régler pour tous les agents à venir ? ». Précision du
-- propriétaire : ameesh ne fait AUCUNE analyse des lots passés à la demande.
-- L'auditeur, à son passage quotidien, analyse les lots livrés et publie une
-- table (`ameesh work estimates --publish`) ; à la création d'un lot, ameesh
-- se contente de lire la dernière publiée pour proposer l'estimation de
-- référence (`work add`, `work show`).
--
-- 1. `work_items.change_size` : taille du changement DÉCLARÉE à la création
--    (`ameesh work add --size s|m|l`), l'une des clés de la table.
-- 2. `estimate_references` : une ligne par publication ; la plus récente est
--    en vigueur. `body` suit le schéma `ameesh-estimate-reference/1` (groupes
--    type × taille × hôte × harnais : médiane et p80 de la durée réelle ;
--    biais de chaque estimateur, et ceux hors de ×0,5–×2, qui lèvent
--    l'alerte `estimate_bias`).

alter table work_items
    add column if not exists change_size text;

alter table work_items drop constraint if exists work_items_change_size_check;
alter table work_items add constraint work_items_change_size_check
    check (change_size is null or change_size in ('s', 'm', 'l'));

create table if not exists estimate_references (
    id           bigserial primary key,
    published_at timestamptz not null default now(),
    published_by text not null default '',
    body         jsonb not null
);

comment on table estimate_references is
  'L157b : table de référence des durées publiée par l''auditeur (ameesh work estimates --publish) ; la plus récente est en vigueur, ameesh la lit sans calcul.';
