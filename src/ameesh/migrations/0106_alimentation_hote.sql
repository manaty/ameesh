-- SPDX-License-Identifier: AGPL-3.0-only
-- 0106_alimentation_hote — alimentation de l'hôte dans les relevés de
-- ressources (lot L106, survivre à une coupure de l'hôte).
--
-- Le 2026-10-10, un portable hôte d'une dizaine d'agents s'est éteint
-- batterie vide (aucun arrêt propre) sans que rien ne l'annonce : les relevés
-- de L31 ne mesuraient ni la batterie ni le secteur. Deux colonnes, nulles
-- quand l'hôte ne les donne pas (serveur, poste fixe, autre système) — on ne
-- devine pas :
--   * `on_ac` : vrai sur secteur, faux sur batterie ;
--   * `battery_percent` : charge des batteries du système, de 0 à 100.
-- Les SEUILS restent déclaratifs (`policy.resources.min_battery_percent`,
-- `stop_battery_percent` de la fiche Host, valeurs par défaut sinon).
--
-- Le numéro 0106 (celui du lot) laisse libres 0043 et suivants aux lots
-- parallèles : les migrations s'appliquent par version manquante.

alter table host_resources add column if not exists on_ac boolean;
alter table host_resources add column if not exists battery_percent double precision;
