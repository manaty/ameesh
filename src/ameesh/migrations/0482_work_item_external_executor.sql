-- SPDX-License-Identifier: AGPL-3.0-only
-- 0482_work_item_external_executor — #3447 : un lot peut porter un exécutant
-- externe déclaré (bureau, owner) sans être assigné.
--
-- Constat du 25/10 : les lots #3443 et #3445 étaient « réservés au bureau »,
-- un agent `externe` (0030) que `work assign` refuse (le forçage `--externe`
-- est réservé à l'owner). Sans assigné, L165 les a pris, et chaque agent a dû
-- les rendre. `external_executor` nomme cet exécutant déclaré : la prise
-- automatique écarte le lot, les alertes et le débit le nomment au lieu de
-- « sans assigné ». Un sous-lot de préparation n'hérite pas du champ : sa
-- part reste assignable.
--
-- NULL : lot ordinaire, rien ne change. Additive seulement : aucune ligne
-- existante ne change de sens.
set local lock_timeout = '5s';

alter table work_items
    add column if not exists external_executor text;

comment on column work_items.external_executor is
    '#3447: declared external executor (an `externe` agent, 0030, or a human) the lot is reserved to; L165 does not take it; NULL = ordinary lot.';
