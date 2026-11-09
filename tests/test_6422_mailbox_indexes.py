# SPDX-License-Identifier: AGPL-3.0-only
"""#6422 — les deux lectures d'agent_mailbox passent par les index partiels 0558.

Sans eux, `human_messages` lisait tout `agent_mailbox_recent_idx`
(732 ms sur 201 k lignes) et `prose_candidates` faisait un Seq Scan par
candidat pour sa sonde `NOT EXISTS` (1,5 s).
"""
from __future__ import annotations

import json
import time
import unittest

from ameesh import db as db_mod
from ameesh import storage
from ameesh.storage.postgres.decisions import PROSE_CANDIDATES_SQL
from ameesh.storage.postgres.mailbox import human_messages_query

from .support import PgTestCase

HUMAN_INDEX = "agent_mailbox_human_chat_recent_idx"
PROSE_INDEX = "agent_mailbox_decision_source_mail_idx"

#: la requête `human_messages` d'avant #6422, telle quelle : la preuve
#: d'équivalence compare ses lignes à celles du nouveau SQL sur le même jeu.
OLD_HUMAN_MESSAGES_SQL = """
    SELECT m.id, m.sender, m.recipient, m.body, m.kind, m.payload,
           m.work_item_id, w.id AS lot_id,
           extract(epoch from m.created_at)::float8 AS created_ts
      FROM agent_mailbox m
      LEFT JOIN work_items w
        ON w.id = CASE WHEN btrim(coalesce(m.work_item_id, '')) ~ '^[0-9]{1,18}$'
                       THEN btrim(m.work_item_id)::bigint END
      LEFT JOIN agent_registry r ON r.name = m.sender
     WHERE m.created_at >= to_timestamp(%s)
       AND (starts_with(m.sender, 'human:')
            OR m.payload @> '{"human": true}'::jsonb
            OR (starts_with(m.sender, 'chat-')
                AND coalesce(r.mode, 'execute') = 'externe')__DECLARED__)
       AND NOT m.payload @> '{"ack": true}'::jsonb
       AND coalesce(m.payload ->> 'cc', '') = ''
       AND NOT (m.kind = 'request' AND (m.payload -> 'decision') IS NOT NULL)
     ORDER BY m.id DESC
     LIMIT %s
"""


def _old_query(*, since_ts, humans, limit):
    names = sorted({str(n) for n in humans or () if str(n).strip()})
    declared = (" OR m.sender IN (%s)" % ", ".join(["%s"] * len(names))) if names else ""
    return (OLD_HUMAN_MESSAGES_SQL.replace("__DECLARED__", declared),
            (float(since_ts), *names, int(limit)))



class IndexDefinitionTest(PgTestCase):
    """Les deux index sont partiels et portent le prédicat attendu."""

    def _defs(self):
        rows = self.db.query(
            "SELECT c.relname, pg_get_indexdef(i.indexrelid) AS def,"
            "       pg_get_expr(i.indpred, i.indrelid) AS pred"
            "  FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
            " WHERE c.relname IN (%s, %s)"
            "   AND c.relnamespace = current_schema()::regnamespace",
            (HUMAN_INDEX, PROSE_INDEX))
        return {row["relname"]: row for row in rows}

    def test_human_and_chat_index_is_partial(self):
        row = self._defs()[HUMAN_INDEX]
        self.assertIn("(created_at DESC)", row["def"])
        self.assertIn("starts_with(sender, 'human:'", row["pred"])
        self.assertIn("payload @> '{\"human\": true}'::jsonb", row["pred"])
        self.assertIn("starts_with(sender, 'chat-'", row["pred"])

    def test_decision_source_mail_index_is_partial(self):
        row = self._defs()[PROSE_INDEX]
        self.assertIn("payload -> 'decision'::text) ->> 'source_mail'::text",
                      row["def"])
        self.assertIn("kind = 'request'::text", row["pred"])
        self.assertIn("payload -> 'decision'::text) IS NOT NULL", row["pred"])
        self.assertIn("payload -> 'decision'::text) ->> 'source_mail'::text) IS NOT NULL",
                      row["pred"])


class PlanTest(PgTestCase):
    """Le plan des deux requêtes vise les index de 0558, sur 100 k lignes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with cls.db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout = 0")
            tx.execute("ALTER TABLE agent_mailbox DISABLE TRIGGER agent_mailbox_notify")
            tx.execute(
                """
                INSERT INTO agent_mailbox
                       (sender, recipient, body, kind, payload, work_item_id,
                        created_at)
                SELECT CASE WHEN g % 100 = 0 THEN 'human:alice'
                            WHEN g % 500 = 0 THEN 'chat-alice'
                            ELSE 'agent-' || (g % 20) END,
                       CASE WHEN g % 100 = 0 THEN 'bureau'
                            WHEN g % 500 = 0 THEN 'orch'
                            WHEN g % 7 = 0 THEN 'human:alice'
                            ELSE 'bureau' END,
                       repeat('x', 120) || g::text,
                       CASE WHEN g % 37 = 0 THEN 'request'
                            WHEN g % 23 = 0 THEN 'reply'
                            WHEN g % 29 = 0 THEN 'event'
                            ELSE 'notify' END,
                       CASE WHEN g % 100 = 0 THEN '{"human": true}'::jsonb
                            WHEN g % 37 = 0 THEN jsonb_build_object(
                                'decision', jsonb_build_object(
                                    'source_mail', (g - 1)::text))
                            ELSE '{}'::jsonb END,
                       CASE WHEN g % 5 = 0 THEN (g % 50)::text END,
                       now() - ((100000 - g) * interval '20 seconds')
                  FROM generate_series(1, 100000) g
                """
            )
            tx.execute("ALTER TABLE agent_mailbox ENABLE TRIGGER agent_mailbox_notify")
        cls.db.execute(
            "INSERT INTO agent_registry (name, harness, host, status, mode)"
            " VALUES ('chat-alice', 'claude', 'test', 'idle', 'externe'),"
            "        ('chat-bob', 'claude', 'test', 'idle', 'execute')"
            " ON CONFLICT (name) DO UPDATE SET mode = excluded.mode")
        cls.db.execute("ANALYZE agent_mailbox")

    def _plan(self, sql, params):
        with self.db.transaction() as tx:
            tx.execute("SET LOCAL enable_seqscan = on")
            return json.dumps(tx.query("EXPLAIN (FORMAT JSON) " + sql, params))

    def test_human_messages_uses_the_partial_index(self):
        if not isinstance(self.db, db_mod.PsycopgDriver):
            self.skipTest("EXPLAIN is wrapped by the psql driver")
        sql, params = human_messages_query(
            since_ts=time.time() - 86400, humans=["smichea"], limit=1000)
        plan = self._plan(sql, params)
        self.assertIn(HUMAN_INDEX, plan)
        self.assertNotIn("agent_mailbox_recent_idx", plan)

    def test_prose_candidates_probes_the_expression_index(self):
        if not isinstance(self.db, db_mod.PsycopgDriver):
            self.skipTest("EXPLAIN is wrapped by the psql driver")
        # Le planificateur peut préférer un Hash Anti Join sur les seules
        # demandes (petites) ; le chemin lent de production est le Nested Loop
        # par candidat, que l'index 0558 doit servir. On force ce chemin pour
        # vérifier que l'expression de l'index est bien celle de la sonde.
        with self.db.transaction() as tx:
            tx.execute("SET LOCAL enable_hashjoin = off")
            tx.execute("SET LOCAL enable_mergejoin = off")
            plan = json.dumps(tx.query(
                "EXPLAIN (FORMAT JSON) " + PROSE_CANDIDATES_SQL,
                (time.time() - 86400, json.dumps(["bureau"]), 500)))
        self.assertIn(PROSE_INDEX, plan)
        self.assertNotIn("Seq Scan on agent_mailbox r", plan)


class _MailFixture(PgTestCase):
    """Un schéma vide et deux agents de chat, pour les petits jeux d'essai."""

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM agent_mailbox")
        self.db.execute(
            "INSERT INTO agent_registry (name, harness, host, status, mode)"
            " VALUES ('chat-alice', 'claude', 'test', 'idle', 'externe'),"
            "        ('chat-bob', 'claude', 'test', 'idle', 'execute')"
            " ON CONFLICT (name) DO UPDATE SET mode = excluded.mode")

    def _send(self, sender, recipient, *, kind="notify", payload=None,
              work_item_id=None, age_s=0.0, delivered=False):
        rows = self.db.query(
            "INSERT INTO agent_mailbox (sender, recipient, body, kind, payload,"
            " work_item_id, created_at, delivered_at)"
            " VALUES (%s, %s, 'corps', %s, %s::jsonb, %s,"
            "         now() - make_interval(secs => %s),"
            "         CASE WHEN %s THEN now() END) RETURNING id",
            (sender, recipient, kind, json.dumps(payload or {}),
             work_item_id, float(age_s), bool(delivered)))
        return int(rows[0]["id"])


class SemanticsTest(_MailFixture):
    """La réécriture de `human_messages` garde exactement les lignes d'avant."""

    def _human(self, humans, *, since_s=86400.0):
        return [int(row["id"]) for row in storage.of(self.db).mailbox.human_messages(
            since_ts=time.time() - since_s, humans=humans, limit=100)]

    def test_human_messages_keeps_the_legacy_declared_names(self):
        marked = self._send("human:alice", "bureau", payload={"human": True})
        declared = self._send("alice", "bureau")
        chat = self._send("chat-alice", "orch")
        self._send("chat-bob", "orch")                     # mode execute : pas humain
        self._send("agent-x", "bureau")                    # un agent : pas humain
        self.assertEqual(self._human(["alice"]), [marked, declared, chat])
        self.assertEqual(self._human([]), [marked, chat])

    def test_human_messages_keeps_the_other_filters(self):
        kept = self._send("human:alice", "bureau", payload={"human": True})
        self._send("human:alice", "bureau", payload={"human": True, "ack": True})
        self._send("human:alice", "bureau", payload={"human": True, "cc": "dev1"})
        self._send("human:alice", "bureau", kind="request",
                   payload={"human": True, "decision": {"question": "?"}})
        self._send("human:alice", "bureau", payload={"human": True}, age_s=172800)
        self.assertEqual(self._human([]), [kept])

    def test_prose_candidates_keeps_the_converted_request_out(self):
        report = self._send("agent-x", "bureau")
        converted = self._send("agent-y", "bureau")
        self._send("agent-z", "bureau", kind="request",
                   payload={"decision": {"source_mail": str(converted)}})
        to_human = self._send("agent-x", "human:bob")
        self._send("ameesh", "bureau")
        self._send("human:alice", "bureau")
        self._send("agent-x", "dev1")
        self._send("agent-x", "bureau", kind="event")
        rows = storage.of(self.db).decisions.prose_candidates(
            recipients=["bureau"], since_ts=time.time() - 86400, limit=100)
        self.assertEqual([int(row["id"]) for row in rows], [report, to_human])


class EquivalenceTest(_MailFixture):
    """Même jeu semé, mêmes lignes : l'ancienne et la nouvelle requête.

    Couvre les quatre branches (human:%, payload human, chat externe, noms
    déclarés), leurs recoupements (une ligne qui satisfait deux branches ne
    sort qu'une fois), une ligne déjà lue, une ligne de chaque branche hors
    fenêtre, et les filtres ack/cc/demande.
    """

    def _seed(self):
        self.db.execute(
            "INSERT INTO work_items (id, title) VALUES (42, 'lot 42')"
            " ON CONFLICT (id) DO NOTHING")
        a = self._send("human:alice", "bureau")                       # A
        b = self._send("agent-x", "bureau", payload={"human": True})  # B
        c = self._send("chat-alice", "orch")                          # C
        d = self._send("alice", "bureau")                             # D
        ab = self._send("human:alice", "bureau", payload={"human": True},
                        work_item_id="42")                            # A+B
        bd = self._send("alice", "bureau", payload={"human": True},
                        work_item_id="abc")                           # B+D
        bc = self._send("chat-alice", "orch", payload={"human": True})  # B+C
        delivered = self._send("human:alice", "bureau",
                               payload={"human": True}, delivered=True)
        request = self._send("human:bob", "bureau", kind="request")
        self._send("human:alice", "bureau",
                   payload={"human": True, "ack": True})
        self._send("human:alice", "bureau",
                   payload={"human": True, "cc": "dev1"})
        self._send("human:alice", "bureau", kind="request",
                   payload={"human": True, "decision": {"question": "?"}})
        self._send("human:alice", "bureau", payload={"human": True},
                   age_s=172800)                                       # hors fenêtre
        self._send("agent-x", "bureau", payload={"human": True},
                   age_s=172800)                                       # hors fenêtre
        self._send("chat-alice", "orch", age_s=172800)                 # hors fenêtre
        self._send("alice", "bureau", age_s=172800)                    # hors fenêtre
        self._send("agent-y", "bureau")                                # un agent
        self._send("chat-bob", "orch")                                 # mode execute
        return [a, b, c, d, ab, bd, bc, delivered, request]

    def _compare(self, humans, limit):
        ids = self._seed()
        since = time.time() - 86400
        new_sql, new_params = human_messages_query(
            since_ts=since, humans=humans, limit=limit)
        old_sql, old_params = _old_query(
            since_ts=since, humans=humans, limit=limit)
        new_rows = [dict(row) for row in self.db.query(new_sql, new_params)]
        old_rows = [dict(row) for row in self.db.query(old_sql, old_params)]
        self.assertEqual(old_rows, new_rows)
        method_rows = [dict(row) for row in
                       storage.of(self.db).mailbox.human_messages(
                           since_ts=since, humans=humans, limit=limit)]
        self.assertEqual(list(reversed(new_rows)), method_rows)
        return ids, [int(row["id"]) for row in method_rows]

    def test_the_four_branches_and_their_overlaps_are_equivalent(self):
        ids, got = self._compare(["alice"], 1000)
        self.assertEqual(got, ids)

    def test_without_declared_names_the_bare_legacy_row_is_out(self):
        ids, got = self._compare([], 1000)
        self.assertEqual(got, [ids[0], ids[1], ids[2], ids[4], ids[5],
                               ids[6], ids[7], ids[8]])

    def test_the_limit_keeps_the_same_newest_rows(self):
        ids, got = self._compare([], 3)
        self.assertEqual(got, ids[6:9])


if __name__ == "__main__":
    unittest.main()
