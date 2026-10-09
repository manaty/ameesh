# SPDX-License-Identifier: AGPL-3.0-only
"""L52 (0032 §2) : historique des sessions d'une persona."""
from __future__ import annotations

from ameesh import persona_sessions as ps, registry

from .support import PgTestCase


class HistoriqueTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "verif-a", harness="deepseek", host="pc", mode="execute")

    def test_session_neuve_puis_remplacee(self):
        registry.set_session(self.db, "verif-a", "s1", "primaire")
        registry.set_session(self.db, "verif-a", "s2", None)
        rows = ps.sessions(self.db, "verif-a")
        self.assertEqual([r["session_id"] for r in rows], ["s2", "s1"])
        self.assertIsNone(rows[0]["ended_at"])
        self.assertEqual(rows[1]["end_reason"], "remplacée par s2")
        self.assertEqual((rows[1]["harness"], rows[1]["host"], rows[1]["account"]),
                         ("deepseek", "pc", "primaire"))

    def test_reprise_de_la_meme_session(self):
        registry.set_session(self.db, "verif-a", "s1", "primaire")
        ps.record_end(self.db, "verif-a", "s1", "arrêt")
        registry.set_session(self.db, "verif-a", "s1", None)
        rows = ps.sessions(self.db, "verif-a")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["ended_at"])
        self.assertEqual(rows[0]["account"], "primaire")     # compte d'origine gardé

    def test_fin_de_session(self):
        registry.set_session(self.db, "verif-a", "s1")
        ps.record_end(self.db, "verif-a", "s1", "rotation")
        ps.record_end(self.db, "verif-a", "s1", "autre")       # déjà close : inchangée
        self.assertEqual(ps.sessions(self.db, "verif-a")[0]["end_reason"], "rotation")
        ps.record_end(self.db, "verif-a", None, "rien")          # sans session : rien

    def test_rotation_close_la_session(self):
        registry.set_session(self.db, "verif-a", "s1")
        self.db.execute("UPDATE agent_registry SET lease_owner = 'r', lease_epoch = 7, "
                        "lease_expires_at = now() + interval '5 minutes' WHERE name = 'verif-a'")
        self.assertTrue(registry.clear_session(self.db, "verif-a", "r", 7))
        self.assertEqual(ps.sessions(self.db, "verif-a")[0]["end_reason"], "rotation")
