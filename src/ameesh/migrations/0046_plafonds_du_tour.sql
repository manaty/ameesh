-- SPDX-License-Identifier: AGPL-3.0-only
-- 0046_plafonds_du_tour — plafonds qui agissent PENDANT un tour (lot L105).
--
-- `turn_max_seconds` : durée maximale d'un tour d'agent mené. Au-delà,
-- l'exécuteur clôt le tour au prochain point sûr (fin de l'appel d'outil en
-- cours) ; le travail reprend au tour suivant, dans la même session.
-- NULL = défaut de l'exécuteur (`AMEESH_TURN_MAX_SECONDS`, 30 min) ;
-- 0 = pas de durée maximale.
--
-- `turn_mail_max` : nombre de messages que le hook de courrier remet pendant
-- un même tour. Les suivants restent pour le tour suivant, et le hook dit à
-- l'agent de conclure son tour. NULL = défaut (`AMEESH_TURN_MAIL_MAX`, 5) ;
-- 0 = pas de borne.
--
-- Posés par `ameesh set <agent> turn_max_seconds=… turn_mail_max=…`.

alter table agent_registry add column if not exists turn_max_seconds bigint
    check (turn_max_seconds is null or turn_max_seconds >= 0);
alter table agent_registry add column if not exists turn_mail_max bigint
    check (turn_mail_max is null or turn_mail_max >= 0);
