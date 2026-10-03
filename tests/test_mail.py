"""Boîte aux lettres : dépôt, lecture, remise, LISTEN/NOTIFY."""
from __future__ import annotations

import time
import unittest

from ameesh import mail
from ameesh import registry
from ameesh.backend import PgBackend

from .support import PgTestCase


class MailTest(PgTestCase):
    def test_envoi_lecture_remise(self):
        registry.upsert(self.db, "beta", harness="codex", host="autre")
        message_id = mail.send(self.db, "alpha", "beta", "bonjour beta", host="laptop")

        non_lus = mail.unread(self.db, "beta")
        self.assertEqual(len(non_lus), 1)
        self.assertEqual(non_lus[0]["id"], message_id)
        self.assertEqual(non_lus[0]["sender"], "alpha")
        self.assertEqual(non_lus[0]["body"], "bonjour beta")
        self.assertEqual(non_lus[0]["status"], "pending")
        self.assertEqual(non_lus[0]["delivered_ts"], None)
        self.assertEqual(mail.unread_counts(self.db), {"beta": 1})

        self.assertEqual(mail.mark_delivered(self.db, [message_id]), 1)
        self.assertEqual(mail.unread(self.db, "beta"), [])
        self.assertEqual(mail.unread_counts(self.db), {})
        self.assertEqual(mail.mark_delivered(self.db, [message_id]), 0)  # déjà remis
        history = mail.history(self.db, "beta")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "delivered")
        self.assertIsNotNone(history[0]["delivered_ts"])

    def test_normalize_forme_commune(self):
        mail.send(self.db, "alpha", "beta", "coucou", host="laptop")
        row = mail.normalize(mail.unread(self.db, "beta")[0])
        self.assertEqual(
            set(row) >= {"id", "from", "to", "ts", "text", "kind", "payload", "host"}, True
        )
        self.assertEqual((row["from"], row["to"], row["text"]), ("alpha", "beta", "coucou"))

    def test_cli_send_all_et_liste(self):
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        registry.upsert(self.db, "beta", harness="codex", host="laptop")
        registry.upsert(self.db, "gamma", harness="deepseek", host="autre")
        backend = PgBackend(self.cfg, self.db)

        targets = backend.send("alpha", "all", "message collectif", host="laptop")
        self.assertEqual(sorted(targets), ["beta", "gamma"])
        self.assertEqual(len(mail.unread(self.db, "beta")), 1)
        self.assertEqual(len(mail.unread(self.db, "gamma")), 1)
        self.assertEqual(mail.unread(self.db, "alpha"), [])

        noms = {row["name"]: row for row in backend.agents()}
        self.assertEqual(noms["beta"]["unread"], 1)
        self.assertEqual(noms["gamma"]["unread"], 1)
        self.assertEqual(noms["alpha"]["unread"], 0)

    def test_listen_notify_de_bout_en_bout(self):
        listener = self.db.listen(["agent_mail"])
        self.assertIsNotNone(listener)
        try:
            time.sleep(0.6)  # laisser LISTEN s'établir (pilote psql : ~0,5 s)
            mail.send(self.db, "alpha", "beta", "réveille-toi", host="laptop")
            deadline = time.monotonic() + 5.0
            recu = None
            while time.monotonic() < deadline and recu is None:
                recu = listener.wait(timeout=1.0)
            self.assertIsNotNone(recu, "aucune notification reçue")
            self.assertEqual(recu["channel"], "agent_mail")
            import json as _json
            charge = _json.loads(recu["payload"])
            self.assertEqual(charge["to"], "beta")
            self.assertEqual(int(charge["id"]), 1)
        finally:
            listener.close()

    def test_notification_par_message(self):
        """Chaque message déposé produit une notification (et pas une par lot)."""
        listener = self.db.listen(["agent_mail"])
        try:
            time.sleep(0.6)
            for index in range(3):
                mail.send(self.db, "alpha", "beta", "message %d" % index)
            payloads = []
            deadline = time.monotonic() + 5.0
            while len(payloads) < 3 and time.monotonic() < deadline:
                item = listener.wait(timeout=1.0)
                if item and item.get("channel"):
                    payloads.append(item["payload"])
            self.assertEqual(len(payloads), 3)
        finally:
            listener.close()


if __name__ == "__main__":
    unittest.main()
