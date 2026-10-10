-- SPDX-License-Identifier: AGPL-3.0-only
--
-- Scission d'un mesh partagé : ne garde que l'organisation visée (lot L57,
-- décision 0033 §1). À lancer sur une COPIE restaurée de la base partagée,
-- jamais sur l'originale :
--
--   psql -v equipes='{nexlink,ameesh}' -v canon_garde='' -f scinder-mesh.sql
--
-- equipes      : équipes (agent_registry.team) des personas gardées ;
-- canon_garde  : identifiant du canon gardé ('' = canon par défaut, NULL en base).
-- Tout le reste (personas, courrier, lots, coûts, liaisons, état de canon) est
-- retiré. Les relevés d'hôte et de comptes, et le catalogue des modèles, sont gardés.
\set ON_ERROR_STOP on
BEGIN;

CREATE TEMP TABLE garde AS
  SELECT name FROM agent_registry
  WHERE team = ANY (:'equipes'::text[])
    AND coalesce(canon, '') = :'canon_garde';
CREATE TEMP TABLE retire AS
  SELECT name FROM agent_registry WHERE name NOT IN (SELECT name FROM garde);

-- courrier : gardé s'il concerne une persona gardée et qu'aucune des deux
-- extrémités n'est une persona retirée (étanchéité, 0033 §1)
DELETE FROM agent_mailbox m
 WHERE NOT coalesce((m.recipient IN (SELECT name FROM garde) OR m.sender IN (SELECT name FROM garde))
                    AND m.recipient NOT IN (SELECT name FROM retire)
                    AND m.sender NOT IN (SELECT name FROM retire), false);

-- lots : ceux des personas gardées et ceux de leurs applications
CREATE TEMP TABLE lots_retires AS
  SELECT id FROM work_items w
   WHERE NOT (coalesce(w.assignee IN (SELECT name FROM garde), false)
              OR (w.assignee IS NULL AND coalesce(w.app = ANY (:'equipes'::text[]), false)));
DELETE FROM action_events WHERE action_id IN
  (SELECT action_id FROM actions WHERE work_item IN (SELECT id FROM lots_retires));
DELETE FROM action_attempts WHERE action_id IN
  (SELECT action_id FROM actions WHERE work_item IN (SELECT id FROM lots_retires));
DELETE FROM actions WHERE work_item IN (SELECT id FROM lots_retires);
UPDATE work_items SET superseded_by = NULL WHERE superseded_by IN (SELECT id FROM lots_retires);
DELETE FROM work_items WHERE id IN (SELECT id FROM lots_retires);   -- événements, jalons, délégations : en cascade

DELETE FROM turn_costs      WHERE agent NOT IN (SELECT name FROM garde);
DELETE FROM turn_resources  WHERE agent NOT IN (SELECT name FROM garde);
DELETE FROM spend_pending   WHERE agent NOT IN (SELECT name FROM garde);
DELETE FROM session_bindings WHERE agent NOT IN (SELECT name FROM garde);
DELETE FROM thread_index    WHERE project <> ALL (:'equipes'::text[])
                              AND project NOT IN (SELECT name FROM garde);
DELETE FROM canon_state          WHERE coalesce(canon, '') <> :'canon_garde';
DELETE FROM authenticator_syncs  WHERE coalesce(canon, '') <> :'canon_garde';
DELETE FROM standing_reservations WHERE grant_id IN (SELECT id FROM standing_approvals
  WHERE authenticator_id IN (SELECT id FROM authenticators WHERE coalesce(canon, '') <> :'canon_garde'));
DELETE FROM standing_approvals WHERE authenticator_id IN
  (SELECT id FROM authenticators WHERE coalesce(canon, '') <> :'canon_garde');
DELETE FROM authenticators       WHERE coalesce(canon, '') <> :'canon_garde';
DELETE FROM work_packages        WHERE coalesce(canon, '') <> :'canon_garde';

DELETE FROM account_switches    WHERE agent NOT IN (SELECT name FROM garde);
DELETE FROM visibility_checks   WHERE persona NOT IN (SELECT name FROM garde);

-- traces gardées qui nomment une persona retirée (auteur d'un événement de lot,
-- dernier auteur d'un fil) : anonymisées, le nom d'un agent d'une autre
-- organisation ne passe pas dans ce mesh (0033 §1, 0015)
-- tout `agent:<nom>` hors des personas gardées (agents disparus du registre
-- compris), et les noms nus des personas retirées
CREATE TEMP TABLE noms_retires AS
  SELECT name AS nom FROM retire UNION SELECT 'agent:' || name FROM retire;
UPDATE work_item_events     SET actor = 'agent:autre-organisation'
 WHERE actor IN (SELECT nom FROM noms_retires)
    OR (actor LIKE 'agent:%' AND substr(actor, 7) NOT IN (SELECT name FROM garde));
UPDATE work_item_milestones SET actor = 'agent:autre-organisation'
 WHERE actor IN (SELECT nom FROM noms_retires)
    OR (actor LIKE 'agent:%' AND substr(actor, 7) NOT IN (SELECT name FROM garde));
UPDATE action_events        SET actor = 'agent:autre-organisation'
 WHERE actor IN (SELECT nom FROM noms_retires)
    OR (actor LIKE 'agent:%' AND substr(actor, 7) NOT IN (SELECT name FROM garde));
UPDATE thread_index         SET last_author = NULL, last_excerpt = ''
 WHERE last_author IN (SELECT nom FROM noms_retires)
    OR (last_author LIKE 'agent:%' AND substr(last_author, 7) NOT IN (SELECT name FROM garde));

DELETE FROM agent_registry WHERE name IN (SELECT name FROM retire);

SELECT 'personas gardées' AS quoi, count(*) FROM agent_registry
UNION ALL SELECT 'messages', count(*) FROM agent_mailbox
UNION ALL SELECT 'lots', count(*) FROM work_items
UNION ALL SELECT 'tours comptés', count(*) FROM turn_costs
UNION ALL SELECT 'traces anonymisées', (SELECT count(*) FROM work_item_events WHERE actor = 'agent:autre-organisation');
COMMIT;
