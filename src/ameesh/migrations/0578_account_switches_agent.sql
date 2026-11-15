-- SPDX-License-Identifier: AGPL-3.0-only
-- 0578_account_switches_agent — lot #7439 : la bascule propre d'un agent
-- critique entre aussi au journal `account_switches`.
--
-- L242b (décision owner du 2026-10-12) : un agent critique (orchestrateur,
-- conception, auditeur) choisit son compte pour lui seul et n'écrit jamais le
-- dernier choix partagé (`account_active`). Sa bascule n'était donc pas
-- journalisée en base : le fil disait « for this critical agent only », mais
-- `account_switches` restait vide, et l'analyse de nuit du 15/11 n'a pas pu
-- relire les 19 changements de compte Claude du 14/11 (lot #7439).
--
-- Le type `agent` distingue cette bascule d'un changement du choix partagé.
-- La contrainte de 0028 s'élargit : aucune valeur existante ne devient
-- invalide, le code d'avant continue de lire la table.
-- ameesh: constraint not tightened

alter table account_switches
    drop constraint if exists account_switches_kind_check;

alter table account_switches
    add constraint account_switches_kind_check
    check (kind in ('bascule', 'retour', 'manuel', 'auto', 'config', 'agent'));

comment on table account_switches is
  'Le journal des bascules de compte : choix partagé (bascule, retour, manuel, auto, config) et choix propre à un agent critique (agent, L242b/#7439).';
