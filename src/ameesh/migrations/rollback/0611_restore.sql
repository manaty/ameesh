-- SPDX-License-Identifier: AGPL-3.0-only
-- 0611_restore — #8347 : retour arrière de la table des réservations de
-- lignée. Les réservations sont transitoires (un tour en vol) : les perdre
-- au retour arrière ne perd aucune dépense, le grand livre fait foi.
drop table if exists budget_lineage_reservations;
