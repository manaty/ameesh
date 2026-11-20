"""Probe (review #3889): a chat key replayed after IDEMPOTENCY_RETENTION_S
(a day) — the reservation is purged; does the action's own trace (the mail)
still prevent a second mail?"""
import dataclasses
import json

from ameesh import registry, serve, serve_actions, storage

from tests.support import PgTestCase


class PurgeProbe(PgTestCase):
    KEY = "web-3889-purge-01"

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE api_idempotency, agent_mailbox, conversation_log,"
                        " conversation_heads CASCADE")
        registry.upsert(self.db, "a1", chantier="demo", harness="codex", host="h1")
        self.ctx = serve.Context(cfg=dataclasses.replace(self.cfg, read_only=True),
                                 human="human:owner")
        self.action, self.params = serve.match_action("/api/v1/agents/a1/chat")
        self.body = {"text": "bonjour"}
        self.digest = serve.fingerprint("/api/v1/agents/a1/chat", self.body)

    def act(self):
        return serve.act(self.ctx, self.db, self.action, self.params, self.body,
                         self.KEY, self.digest)

    def mails(self):
        return self.db.query("select id, payload::text as payload from agent_mailbox"
                             " order by id")

    def test_rejeu_apres_la_purge_de_la_reservation(self):
        status, data, replayed = self.act()
        self.assertEqual((status, replayed), (200, False))
        self.assertEqual(len(self.mails()), 1)
        # 24 h plus tard : `reserve` purge la réservation (IDEMPOTENCY_RETENTION_S)
        self.db.execute("delete from api_idempotency")
        status, data, replayed = self.act()
        print("REPLAY-AFTER-PURGE:", status, data.get("error"), replayed,
              "mails:", len(self.mails()))
        self.assertEqual(len(self.mails()), 2,
                         "the action ran again under the same key: second mail")


class InFlightProbe(PgTestCase):
    """Probe (review #3889): a first action still running after
    RECONCILE_AFTER_S — the reservation is reconciled (no trace yet) and the
    action runs a second time under the same key."""
    KEY = "web-3889-inflight-01"

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE api_idempotency")
        self.ctx = serve.Context(cfg=dataclasses.replace(self.cfg, read_only=True),
                                 human="human:owner")

    def test_action_encore_en_cours_apres_120s(self):
        import threading
        started = threading.Event()
        release = threading.Event()
        runs = []

        def build(ctx, db, params, body):
            runs.append(1)
            if len(runs) == 1:
                started.set()
                release.wait(10)
            return {"run": len(runs)}

        action = serve_actions.Action("/x", "x", "x", build, {},
                                      outcome=lambda *a: None)
        digest = serve.fingerprint("/x", {})
        first = []

        def one():
            first.append(serve.act(self.ctx, self.db, action, {}, {}, self.KEY, digest))
        thread = threading.Thread(target=one)
        thread.start()
        self.assertTrue(started.wait(5))
        self.db.execute("update api_idempotency set created_at = now()"
                        " - interval '121 seconds'")
        second = serve.act(self.ctx, self.db, action, {}, {}, self.KEY, digest)
        release.set()
        thread.join(10)
        print("IN-FLIGHT:", "runs:", len(runs), "second:", second[0],
              "first:", first[0][0] if first else None)
        self.assertEqual(len(runs), 2, "the same key ran the action twice")
