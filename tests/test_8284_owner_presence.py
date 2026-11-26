# SPDX-License-Identifier: AGPL-3.0-only
"""#8284 (issue manaty/ameesh#6518) : une note ou un courrier écrit depuis le
poste déclaré d'un humain du canon réveille le bureau et le pilote avec ses
gestes en attente ; un accord conversationnel demande la signature native ;
l'acteur « inconnu » du poste est marqué comme tel."""
import types
import unittest
from unittest import mock

from ameesh import owner_presence, registry, work

from .support import PgTestCase

APP = "t8284"
HERE = owner_presence.Station(human="human:owner", account="owner@pc-owner")
DECISION = {"id": 233821, "lot": 8054, "question": "Go prod 11ce ?",
            "needs_signature": True, "urgent": False, "overdue": False}


def _canon(*members):
    return types.SimpleNamespace(members=[
        types.SimpleNamespace(title=title, local_accounts=accounts)
        for title, accounts in members])


class StationTest(unittest.TestCase):
    def station(self, proof, *members):
        with mock.patch("ameesh.decisions.human_account_proof", return_value=proof), \
                mock.patch("ameesh.canon.load_configured", return_value=[_canon(*members)]):
            return owner_presence.station(None, None)

    def test_declared_account_is_the_owner_station(self):
        self.assertEqual(self.station("owner@pc-owner", ("owner", ["owner@pc-owner"]),
                                      ("other", ["x@y"])), HERE)

    def test_bound_session_unknown_or_ambiguous_account_is_no_station(self):
        self.assertIsNone(self.station("", ("owner", ["owner@pc-owner"])))
        self.assertIsNone(self.station("ameesh@ag3", ("owner", ["owner@pc-owner"])))
        self.assertIsNone(self.station("owner@pc-owner", ("owner", ["owner@pc-owner"]),
                                       ("twin", ["owner@pc-owner"])))

    def test_mark_names_the_station_and_accord_cues(self):
        self.assertEqual(owner_presence.mark("retour en séance", HERE),
                         "retour en séance [poste de human:owner : owner@pc-owner]")
        self.assertEqual(owner_presence.mark("x", None), "x")
        self.assertTrue(owner_presence.is_accord(
            "Accord conversationnel uniquement ; signature native attendue"))
        self.assertTrue(owner_presence.is_accord("accord utilisateur reçu sur ce chat"))
        self.assertFalse(owner_presence.is_accord("pas d'accord sur la conception"))


class RelayTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("bureau", "pilote"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host,
                            cwd=self.tmp, mode="execute")
        self.lot = work.add(self.db, title="Go prod 11ce", app=APP, actor="pilote",
                            cfg=self.cfg)
        patches = [
            mock.patch.object(owner_presence, "recipients", return_value=["bureau", "pilote"]),
            mock.patch.object(owner_presence, "_decisions", return_value=[DECISION]),
            mock.patch.object(owner_presence, "_logins", return_value=["ag3/claude/primaire"]),
            mock.patch.object(owner_presence, "_sessions", return_value=[]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def mails(self, name):
        return self.db.query(
            "SELECT body, payload FROM agent_mailbox WHERE recipient = %s"
            " AND payload ? 'owner_presence' ORDER BY id", (name,))

    def relay(self, text, now):
        return owner_presence.relay(self.cfg, self.db, HERE, source="note", text=text,
                                    lot=self.lot["id"], now=now)

    def test_presence_wakes_bureau_and_pilot_once_per_window(self):
        now = 1_800_000_000.0
        self.assertEqual(self.relay("retour de l'owner en séance PC", now),
                         ["bureau", "pilote"])
        for name in ("bureau", "pilote"):
            [row] = self.mails(name)
            self.assertTrue(row["payload"]["urgent"])
            self.assertIn("décision #233821 (#8054) [signature]", row["body"])
            self.assertIn("ag3/claude/primaire", row["body"])
            self.assertNotIn("Accord conversationnel relevé", row["body"])
        # même fenêtre, autre texte : déjà relayé
        self.assertEqual(self.relay("toujours là", now + 60), [])
        self.assertEqual(len(self.mails("bureau")), 1)
        # fenêtre suivante : relayé de nouveau
        self.assertEqual(self.relay("toujours là", now + owner_presence.WINDOW_S),
                         ["bureau", "pilote"])

    def test_accord_asks_native_signature_even_within_the_window(self):
        now = 1_800_000_000.0
        self.relay("retour en séance", now)
        text = ("accord utilisateur reçu sur ce chat : choix A à #233821. "
                "Accord conversationnel uniquement ; signature native attendue")
        self.assertEqual(self.relay(text, now + 60), ["bureau", "pilote"])
        row = self.mails("bureau")[-1]
        self.assertIn("Accord conversationnel relevé (#233821)", row["body"])
        self.assertIn("ameesh decide", row["body"])
        self.assertEqual(row["payload"]["owner_presence"]["accord"], [233821])

    def test_no_station_relays_nothing(self):
        self.assertEqual(owner_presence.relay(self.cfg, self.db, None, source="note",
                                              text="x", lot=self.lot["id"]), [])


class UnpatchedTest(PgTestCase):
    def test_gestures_and_recipients_read_the_real_base(self):
        gestures = owner_presence.pending_gestures(self.cfg, self.db, "human:owner")
        self.assertEqual(set(gestures), {"decisions", "logins", "sessions"})
        lot = work.add(self.db, title="x", app=APP, actor="pilote", cfg=self.cfg)
        self.assertIsInstance(
            owner_presence.recipients(self.cfg, self.db, "human:owner", lot), list)

    def test_cli_note_from_an_undeclared_station_is_unchanged(self):
        lot = work.add(self.db, title="x", app=APP, actor="pilote", cfg=self.cfg)
        proc = self.mesh("work", "note", str(lot["id"]), "retour en séance",
                         "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        notes = [e["note"] for e in work.events(self.db, lot["id"], None)]
        self.assertIn("retour en séance", notes)
        self.assertNotIn("owner presence relayed", proc.stderr)


if __name__ == "__main__":
    unittest.main()
