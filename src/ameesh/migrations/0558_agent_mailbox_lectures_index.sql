-- SPDX-License-Identifier: AGPL-3.0-only
-- 0558_agent_mailbox_lectures_index — #6422 : les deux lectures d'agent_mailbox
-- qui balayaient la table passent par des index partiels.
--
-- Constat (analyse de nuit du 09/11, EXPLAIN ANALYZE en lecture seule sur
-- origin/main 1c403005b ; base centrale : 201 833 lignes, 467 Mo) :
--   * `human_messages` (storage/postgres/mailbox.py, `ameesh work unrecorded`)
--     filtrait created_at sans destinataire : lecture entière de
--     `agent_mailbox_recent_idx (recipient, created_at)`, 732 ms ;
--   * `prose_candidates` (storage/postgres/decisions.py, `ameesh decisions`)
--     cherchait `payload->'decision'->>'source_mail'` sans index : Seq Scan
--     par ligne candidate, 1,5 s.
--
-- Deux index partiels, additifs, sur les seules lignes que ces lectures
-- regardent (336 lignes humaines et 55 lignes de chat sur 201 833 ; 19 lignes
-- porteuses de `source_mail`) : le coût d'écriture du courrier ne change
-- quasiment pas.
--
-- Construction simple, pas `CONCURRENTLY` : `migrate()` exécute toute la passe
-- dans une transaction et Postgres refuse un build concurrent dans un bloc
-- (même choix que 0413, 0489, 0495, 0497 et 0511 sur cette table). L'attente
-- de verrou est bornée à 5 s (posée aussi par `migrate()`, L835) ; le build
-- lui-même est court : 170 ms et 109 ms mesurés sur une copie de 201 000
-- lignes / 446 Mo. Si un écrivain tient la table plus de 5 s, la migration
-- échoue et reste en attente, sans faire la queue devant tous les écrivains.
SET LOCAL lock_timeout = '5s';

-- human_messages : le prédicat reprend, au mot près, les branches indexables
-- de la requête (storage/postgres/mailbox.py). Il couvre aussi les lignes
-- `chat-%`, dont la requête a besoin pour sa branche « chat d'un humain
-- externe ». La branche des noms humains déclarés, elle, reste sur
-- `agent_mailbox_sender_idx (sender, id DESC)` : un prédicat partiel ne peut
-- pas dépendre du contenu de la configuration.
CREATE INDEX IF NOT EXISTS agent_mailbox_human_chat_recent_idx
    ON agent_mailbox (created_at DESC)
    WHERE starts_with(sender, 'human:')
       OR payload @> '{"human": true}'::jsonb
       OR starts_with(sender, 'chat-');

-- prose_candidates : la sonde NOT EXISTS compare l'id du message à
-- `payload->'decision'->>'source_mail'` des seules demandes de décision.
-- L'index d'expression porte exactement cette valeur ; les deux prédicats
-- gardent les seules lignes utiles (19 sur 201 833 portent un source_mail).
CREATE INDEX IF NOT EXISTS agent_mailbox_decision_source_mail_idx
    ON agent_mailbox ((payload -> 'decision' ->> 'source_mail'))
    WHERE kind = 'request' AND (payload -> 'decision') IS NOT NULL
      AND (payload -> 'decision' ->> 'source_mail') IS NOT NULL;

COMMENT ON INDEX agent_mailbox_human_chat_recent_idx IS
    '#6422 partial index of human and chat-origin mail, read newest first by human_messages; declared bare human names stay on agent_mailbox_sender_idx.';
COMMENT ON INDEX agent_mailbox_decision_source_mail_idx IS
    '#6422 expression index of the source mail named by a decision request, probed by prose_candidates anti-join.';
