-- SPDX-License-Identifier: AGPL-3.0-only
-- 0485_role_standings_notify — #3568 : la passe alerts ne dépend plus d'un
-- détenteur sans route de poussée.
--
-- #3383 a fait de la livraison des alertes une minuterie de rôle : le
-- détenteur du bail mesh-design la lance sur ses propres routes. #3402 a
-- gardé le manque de route comme un avertissement (le secret absent ne fait
-- pas échouer `role timers --check` ni le contrôle de déploiement). Mais le
-- bail peut échoir à un hôte qui n'a aucune route : la passe alerts s'arrête
-- alors que d'autres candidats savent pousser (incident du 25/10/2026 :
-- agents-1 détenteur sans ntfy, agents-2 classé 0 et en attente).
--
-- Chaque hôte candidat publie donc, avec son relevé de candidature, sa
-- capacité LOCALE à pousser vers les humains requis (`notify_reach` : route
-- distante ntfy/slack, secret résolu dans l'environnement de l'unité). Le
-- lecteur en déduit le délégué de la passe alerts : le détenteur s'il peut
-- pousser, sinon le premier candidat frais qui le peut (`election.
-- alerts_delegate`). Additif seulement : les lignes existantes ne changent
-- pas de sens, et `notify_ok=false` par défaut ne rend personne inéligible
-- au bail (le manque de secret reste un avertissement, #3402).

alter table role_standings
    add column if not exists notify_ok boolean not null default false,
    add column if not exists notify_diagnostic text not null default '';

comment on column role_standings.notify_ok is
    '#3568: the candidate host can push an alert to every required human through a remote channel (ntfy/slack) with the secret resolved; used only to choose the alerts-pass delegate, never to make the role lease ineligible.';

comment on column role_standings.notify_diagnostic is
    '#3568: why notify_ok is false (human without route, unresolved secret), empty when it is true; never a secret value.';
