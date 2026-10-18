-- SPDX-License-Identifier: AGPL-3.0-only
-- 0435_work_close_done — terminer un lot sans branche par `done` (#1280).
--
-- Un lot de relecture après fusion (`source`/`issue_ref`
-- review-after-merge:#<parent>) qui a livré son verdict n'a ni fusion à
-- enregistrer, ni abandon à assumer, ni remplaçant : c'est le parent qui a
-- été relu. La contrainte de valeur de 0026 n'admettait que
-- ('abandoned','superseded'), élargie par 0430 à 'delivered' ; `done` s'y
-- ajoute. La cohérence état/raison (work_items_closed_reason_check) et l'état
-- terminal `closed` restent inchangés : `done` est une raison de fermeture,
-- pas un état.
--
-- Remplace la contrainte de 0430 par une valeur de plus (L151) : `null` pour
-- un lot ouvert, et les quatre raisons de fermeture.

alter table work_items drop constraint if exists work_items_close_reason_check;
alter table work_items add constraint work_items_close_reason_check
    check (close_reason is null or close_reason in ('abandoned', 'superseded', 'delivered', 'done'));

comment on column work_items.close_reason is
  'Fermeture explicite (L29, #988, #1280) : abandoned, superseded (par superseded_by), delivered (livré hors cible, jalon delivered avec le SHA relu) ou done (livré sans fusion, sans branche).';
