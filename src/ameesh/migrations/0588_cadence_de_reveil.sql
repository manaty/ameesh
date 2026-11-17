-- SPDX-License-Identifier: AGPL-3.0-only
-- #7632 (manaty/ameesh#5822) : cadence de réveil déclarée par la fiche d'un
-- agent (spec avatars §4.4, décision owner du 17/11). `reveil` porte la
-- politique lue par l'exécuteur (`reveil.parse`) :
-- {"immediat": ["humain", "urgent", …], "fenetre_s": 1800}. Les natures
-- listées ouvrent un tour tout de suite ; le reste est regroupé et remis au
-- tour suivant, au plus tard à la fin de la fenêtre. NULL = aucune
-- déclaration : réveil par courrier de l'exécuteur, inchangé. Pas de CHECK
-- en ligne (le préflight de staging la classerait inconnue) : le lecteur
-- (`reveil.of`) tient une valeur qui n'est pas un objet pour une absence.
alter table agent_registry
    add column if not exists reveil jsonb;
