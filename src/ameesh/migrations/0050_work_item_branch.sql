-- SPDX-License-Identifier: AGPL-3.0-only
-- 0050_work_item_branch — la branche d'un lot et sa cible (lot L118).
--
-- Le 2026-10-10, les lots d'un orchestrateur restaient ouverts après la
-- fusion de leur branche : l'équipe fusionne directement sur la branche cible,
-- sans PR, et sans déclarer de gel ; ni `sync-github` ni `sync-merges` (L29)
-- n'avaient de quoi constater la fusion. Un lot peut désormais porter :
--   * `branch` : sa branche de travail (`agent/…`) ;
--   * `branch_target` : la branche où elle doit entrer (nulle : la cible par
--     défaut du dépôt, voir `plan_git.default_target`) ;
--   * `branch_head` : le dernier commit de la branche vu EN AVANCE sur sa
--     cible par le relevé périodique. Il prouve qu'il y a eu du travail
--     (une branche tout juste créée est déjà « ancêtre » de sa cible) et
--     permet de constater la fusion après la suppression de la branche.

alter table work_items add column if not exists branch text;
alter table work_items add column if not exists branch_target text;
alter table work_items add column if not exists branch_head text;
create index if not exists work_items_branch_idx on work_items (branch)
    where branch is not null;
