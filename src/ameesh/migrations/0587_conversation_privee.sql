-- SPDX-License-Identifier: AGPL-3.0-only
-- 0587_conversation_privee — #7668 : une conversation privée n'a pas de fil
-- de projet.
--
-- Un avatar (ou un agent dont la fiche ne déclare aucun projet) garde sa
-- conversation dans `conversation_log` : `project` NULL dit qu'aucune copie
-- R12 ne doit être publiée, hors du lot qu'il traite. La contrainte de longueur
-- existante laisse passer NULL (un CHECK est satisfait sur NULL) ; seule
-- l'interdiction de NULL tombe.
--
-- Additive : le code d'avant n'écrit jamais NULL ; la colonne accepte en plus
-- une valeur, aucune ligne existante ne change de sens.
set local lock_timeout = '5s';

alter table conversation_log alter column project drop not null;

comment on column conversation_log.project is
    '#7668: project of the readable R12 feed; NULL for a private conversation (avatar, or an agent whose card declares no project): entries stay in conversation_log and are never published to a project feed outside the lot being processed.';
