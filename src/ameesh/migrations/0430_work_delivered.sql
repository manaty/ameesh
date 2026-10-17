-- SPDX-License-Identifier: AGPL-3.0-only
-- 0430_work_delivered — la clôture d'un lot livré hors cible (#988, ex-#531).
--
-- Un lot documentaire (rapport, note, répétition) peut être livré sans être
-- fusionné dans la cible : son livrable est la tête relue d'une branche.
-- `ameesh work close <id> --delivered --sha <tête relue>` le ferme avec
-- `close_reason = 'delivered'` et écrit le jalon `delivered` (le SHA relu,
-- l'auteur) ; ce jalon compte comme livraison dans `ameesh work estimates`.
--
-- Élargit les deux contraintes de 0026 (mêmes noms) : un remplacement qui
-- n'accepte que des valeurs de plus reste additif (L151).

alter table work_items drop constraint if exists work_items_close_reason_check;
alter table work_items add constraint work_items_close_reason_check
    check (close_reason is null or close_reason in ('abandoned', 'superseded', 'delivered'));

alter table work_item_milestones drop constraint if exists work_item_milestones_kind_check;
alter table work_item_milestones add constraint work_item_milestones_kind_check
    check (kind in ('requested', 'frozen', 'verdict', 'merged', 'closed', 'delivered'));

-- au plus un jalon `delivered` par lot, comme `closed`
create unique index if not exists work_item_milestones_delivered_idx
    on work_item_milestones (work_item_id) where kind = 'delivered';

comment on column work_items.close_reason is
  'Fermeture explicite (L29, #988) : abandoned, superseded (par superseded_by) ou delivered (livré hors cible, jalon delivered avec le SHA relu).';
