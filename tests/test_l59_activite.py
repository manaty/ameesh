# SPDX-License-Identifier: AGPL-3.0-only
"""L59 (0033 §9, étude v2 G1) : journal d'activité d'une persona."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ameesh import activity, mail, registry, work

from .support import PgTestCase


class DureeTest(unittest.TestCase):
    def test_durees(self):
        self.assertEqual(activity.parse_since("30m"), 1800)
        self.assertEqual(activity.parse_since("24h"), 86400)
        self.assertEqual(activity.parse_since("7d"), 7 * 86400)
        self.assertEqual(activity.parse_since("2j"), 2 * 86400)
        with self.assertRaises(ValueError):
            activity.parse_since("demain")


class JournalTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "ouvrier", harness="deepseek", host="pc", mode="execute")
        registry.upsert(self.db, "coord", harness="claude", host="pc", mode="execute")
        registry.upsert(self.db, "autre", harness="claude", host="pc", mode="execute")
        self.db.execute("DELETE FROM turn_costs WHERE agent = %s", ("ouvrier",))

    def test_chronologie_fusionnee(self):
        mail.send(self.db, "coord", "ouvrier", "prends le lot")
        mail.send(self.db, "ouvrier", "coord", "c'est fait")
        mail.send(self.db, "autre", "coord", "sans rapport")
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, turn, model, session, usd, input_tokens, "
            "output_tokens) VALUES ('ouvrier', 'deepseek', 't1', 'deepseek-flash', 's1', 0.25, "
            "100, 50)")
        lot = work.add(self.db, title="refonte", assignee="ouvrier", actor="coord")
        work.move(self.db, lot["id"], "build", actor="ouvrier")
        items = activity.events(self.db, "ouvrier", 3600)
        kinds = [e["kind"] for e in items]
        self.assertIn("message reçu", kinds)
        self.assertIn("message envoyé", kinds)
        self.assertIn("tour", kinds)
        self.assertIn("lot", kinds)
        self.assertFalse(any("sans rapport" in e["text"] for e in items))
        self.assertEqual([e["at"] for e in items], sorted(e["at"] for e in items))
        tour = [e for e in items if e["kind"] == "tour"][0]
        self.assertIn("0.2500 $", tour["text"])

    def test_hors_periode(self):
        mail.send(self.db, "coord", "ouvrier", "ancien")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '3 days'")
        self.assertEqual(activity.events(self.db, "ouvrier", 86400), [])
        self.assertEqual(len(activity.events(self.db, "ouvrier", 4 * 86400)), 1)

    def test_commande(self):
        mail.send(self.db, "coord", "ouvrier", "bonjour")
        out = io.StringIO()
        with mock.patch("ameesh.config.load", return_value=self.cfg), \
                mock.patch("ameesh.db.connect", return_value=_SansFermer(self.db)), \
                redirect_stdout(out):
            code = activity.main(["ouvrier", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["present"]["name"], "ouvrier")
        self.assertEqual(data["events"][0]["kind"], "message reçu")
        with mock.patch("ameesh.config.load", return_value=self.cfg), \
                mock.patch("ameesh.db.connect", return_value=_SansFermer(self.db)), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(activity.main(["inconnue"]), 2)


class _SansFermer:
    """La base du test reste ouverte quand la commande la « ferme »."""
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


if __name__ == "__main__":
    unittest.main()
