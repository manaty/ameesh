-- SPDX-License-Identifier: AGPL-3.0-only
-- #7833 : une reprise après arrêt du runner du poste porte sa propre raison
-- (`failover`). Le retour automatique par présence (B2) ne doit pas la
-- défaire : sans cette distinction, le poste encore fautif reprenait l'avatar
-- toutes les 120 s, qui re-échouait et repartait au cloud (ping-pong).
-- Additif : seule la liste des valeurs s'élargit ; le code d'avant ne lit que
-- `preference` et `manual` et ignore une ligne `failover`.
set local lock_timeout = '5s';

-- ameesh: constraint not tightened (la liste des raisons s'élargit)
alter table conversation_moves
    drop constraint if exists conversation_moves_reason_check;
alter table conversation_moves
    add constraint conversation_moves_reason_check
    check (reason in ('preference', 'manual', 'failover'));
