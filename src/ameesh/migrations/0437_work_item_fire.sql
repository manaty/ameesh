-- SPDX-License-Identifier: AGPL-3.0-only
-- 0437_work_item_fire — le feu du pilote au gel (canon §2, lot #1374).
--
-- Le pilote donne son feu **au gel**, sur la tête exacte du lot
-- (`ameesh work fire <lot> --sha <commit>`), avant les relectures : la fusion
-- part alors d'elle-même dès que la CI requise est verte, sans attendre le tour
-- d'un agent. Deux jalons portent ce feu :
--
--   fire     le feu est donné, sur le `sha` du gel qu'il autorise ;
--   unfire   le feu est retiré (« retrait » du pilote) ou invalidé par ameesh
--            (la tête a bougé : le feu est à redonner) — même `sha` que le feu
--            porté, pour la trace.
--
-- Le jalon `frozen` reste la seule autorité sur la tête gelée : un feu ne vaut
-- que pour le gel courant, jamais pour un regel. `ameesh.work.milestone`
-- n'écrit ces deux kinds que par la CLI du feu, après contrôle du gel.
--
-- Reprise : la contrainte de 0013/0026 est remplacée, aucun jalon n'est réécrit.
-- Composition paquet 11h (#1374 x #988) : 0430 a déjà élargi la contrainte à
-- `delivered` ; ce remplacement ne garde que des valeurs de plus (L151), la
-- liste reprend donc `delivered` en plus des kinds du feu.

alter table work_item_milestones
    drop constraint if exists work_item_milestones_kind_check;

alter table work_item_milestones
    add constraint work_item_milestones_kind_check
    check (kind in ('requested', 'frozen', 'verdict', 'fire', 'unfire', 'merged', 'closed', 'delivered'));

comment on table work_item_milestones is
  'L10 (R19) : les jalons d''un lot — requested et merged automatiques (trigger), frozen, verdict, fire et unfire déclarés par la CLI. Une ligne par jalon, dans l''ordre où ils arrivent.';
