# SPDX-License-Identifier: AGPL-3.0-only
"""#7658 : un avatar n'est jamais endormi par la file de découpage.

Le défaut D1 de la qualification #7635 : `avatar-essai` (bloc `avatar`,
`releve: preference`, `team: ameesh`) recevait la pause « roadmap: épuisée »
et seul un courrier du pilote ou d'un orchestrateur pouvait le réveiller ; un
courrier de son humain ne le joignait pas. Un agent dont la fiche porte un
bloc `avatar` est donc épargné par `_sleep`, et la pause automatique déjà
posée est levée par `_wake_work`, même hors de son équipe.
"""
import time
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from ameesh import agent_pause, file_decoupage as fd, registry, work

from .support import PgTestCase
from . import test_4610_sleep_work_mail as fixtures


def card(name: str, *, avatar: bool = True):
    """Une fiche minimale : `avatar` n'est lu que par sa présence et sa valeur."""
    data = {"avatar": {"conversation": "portable"}} if avatar else {}
    return NS(title=name, fiche=NS(data=data))


def canon(*names: str, avatars=("dev1",)):
    return NS(agents=[card(name, avatar=name in avatars) for name in names])


class AvatarCardTest(unittest.TestCase):
    def test_the_block_is_read_from_the_card_not_the_role(self):
        def sheet(name, data):
            return NS(title=name, fiche=NS(data=data))

        canons = [NS(agents=[
            sheet("avatar", {"avatar": {"conversation": "portable"}}),
            sheet("plain", {}),
            sheet("empty", {"avatar": None}),
        ])]
        self.assertEqual(fd._avatar_agents(canons), {"avatar"})

    def test_no_canon_means_no_avatar(self):
        self.assertEqual(fd._avatar_agents(None), set())
        self.assertEqual(fd._avatar_agents([]), set())


class AvatarSleepTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("pilote", "dev0", "dev1", "dev2"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host,
                            cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice',"
                        " team = 'ameesh'")
        pilots = mock.patch("ameesh.alert_briefs.project_pilots", return_value=["pilote"])
        pilots.start()
        self.addCleanup(pilots.stop)
        self.state: dict = {}
        self.t0 = time.time()

    # Les fixtures #4610 portent le lot de découpage et le sommeil ; on ne
    # remplace que `run_pass`, qui doit recevoir les canons.
    listing = fixtures.SleepWorkMailTest.listing
    split_lot = fixtures.SleepWorkMailTest.split_lot
    make_split = fixtures.SleepWorkMailTest.make_split
    notes = fixtures.SleepWorkMailTest.notes
    sleep_all = fixtures.SleepWorkMailTest.sleep_all
    lift_event = fixtures.SleepWorkMailTest.lift_event

    def run_pass(self, offset, listing=None, canons=()):
        return fd.run(None, self.db, now=self.t0 + offset, state=self.state,
                      listing=listing, canons=list(canons),
                      settings=fixtures.SETTINGS)

    def test_avatar_is_spared_when_the_roadmap_is_exhausted(self):
        split = self.make_split()
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        out = self.run_pass(1900, self.listing("dev0", "dev1"),
                            canons=[canon("dev0", "dev1")])
        sleep = next(g for g in out if g["kind"] == "sleep")
        by_agent = {row["agent"]: row for row in sleep["agents"]}
        self.assertFalse(by_agent["dev1"]["slept"])
        self.assertIn("avatar", by_agent["dev1"]["why"])
        self.assertTrue(by_agent["dev0"]["slept"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertTrue(registry.get(self.db, "dev0")["operator_paused"])
        notes = [n for n in self.notes(split) if n.startswith("sleep exempted: dev1:")]
        self.assertEqual(len(notes), 1)
        self.assertIn("avatar", notes[0])

    def test_existing_automatic_sleep_is_lifted_and_journalled(self):
        split = self.sleep_all()
        out = self.run_pass(2000, canons=[canon("dev0", "dev1")])
        self.assertEqual([g["agents"] for g in out if g["kind"] == "wake"],
                         [["dev1"]])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertTrue(registry.get(self.db, "dev0")["operator_paused"])
        self.assertIn("avatar", self.lift_event("dev1")["reason"])
        self.assertTrue(any("sleep exempted: dev1:" in note and "avatar" in note
                            for note in self.notes(split)))

    def test_avatar_out_of_the_team_is_lifted_too(self):
        # Complément #7635 : `_wake` ne parcourt que les agents du projet ; un
        # avatar sorti de son équipe gardait la pause jusqu'à son échéance.
        self.sleep_all()
        row = dict(registry.get(self.db, "dev1"))
        row["projects"] = []
        row["team"] = None
        row["unread"] = 0
        out = fd._wake_work(self.db, [row], self.t0 + 2000,
                            canons=[canon("dev0", "dev1")])
        self.assertEqual([g["agents"] for g in out], [["dev1"]])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])

    def test_human_pause_is_preserved(self):
        agent_pause.change(self.db, "dev1", paused=True, reason="maintenance",
                           actor="human:alice", principal="human:alice")
        self.run_pass(2000, canons=[canon("dev0", "dev1")])
        self.assertTrue(registry.get(self.db, "dev1")["operator_paused"])


if __name__ == "__main__":
    unittest.main()
