# SPDX-License-Identifier: AGPL-3.0-only
"""#4623 — la dernière note de feuille de route fait foi.

Témoins : `roadmap: épuisée <projet>` endort ; une `roadmap: rouverte <projet>`
postérieure désarme le sommeil et lève les dormeurs du projet dans la même
passe, même quand tous les exécutants du projet dorment (aucune mesure) ;
`rouverte` puis `épuisée` endort de nouveau ; l'alerte `roadmap_exhausted` suit
la dernière note ; la levée est journalisée (auteur, note, agents) une fois.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from ameesh import file_decoupage as fd
from ameesh import mechanics, registry, work

from .support import PgTestCase

SETTINGS = mechanics.QueueFill(repos_part=0.20, repos_s=1800)


def agent(name, *, idle=True, project="ameesh", **extra):
    row = {"name": name, "mode": "execute", "status": "idle", "lease_live": True,
           "lease_owner": "runner:h1", "unread": 0, "assigned_lot_id": None,
           "active_lots": 0, "actionable_lots": 0 if idle else 1,
           "projects": [project], "responsible": "human:alice"}
    row.update(extra)
    return row


def team(idle: int, busy: int, project="ameesh", prefix="dev"):
    return ([agent("%s%d" % (prefix, i), project=project) for i in range(idle)]
            + [agent("%sb%d" % (prefix, i), idle=False, project=project)
               for i in range(busy)])


class RoadmapStateTest(unittest.TestCase):
    def test_exhausted_then_reopened_is_not_exhausted(self):
        notes = [{"id": 1, "note": "roadmap: épuisée ameesh (ROADMAP.md)",
                  "actor": "pilote"},
                 {"id": 2, "note": "roadmap: rouverte ameesh", "actor": "pilote"}]
        self.assertFalse(fd.exhausted(notes, "ameesh"))
        self.assertEqual(fd.roadmap_state(notes, "ameesh"), fd.REOPENED_STATE)

    def test_reopened_then_exhausted_is_exhausted(self):
        notes = [{"id": 1, "note": "roadmap: rouverte ameesh", "actor": "pilote"},
                 {"id": 2, "note": "roadmap: épuisée ameesh", "actor": "pilote"}]
        self.assertTrue(fd.exhausted(notes, "ameesh"))
        self.assertEqual(fd.roadmap_state(notes, "ameesh"), fd.EXHAUSTED_STATE)

    def test_a_note_for_another_project_does_not_change_the_state(self):
        notes = [{"id": 1, "note": "roadmap: épuisée ameesh", "actor": "pilote"},
                 {"id": 2, "note": "roadmap: rouverte nexlink", "actor": "pilote"}]
        self.assertTrue(fd.exhausted(notes, "ameesh"))
        self.assertEqual(fd.roadmap_state(notes, "nexlink"), fd.REOPENED_STATE)

    def test_the_declared_reopened_spellings_are_equivalent(self):
        for note in ("roadmap: rouverte ameesh", "roadmap: réouverte ameesh"):
            with self.subTest(note=note):
                self.assertEqual(fd.roadmap_state([{"id": 1, "note": note}], "ameesh"),
                                 fd.REOPENED_STATE)

    def test_the_journal_order_decides_not_the_list_order(self):
        # Dans l'ordre reçu, la dernière note est l'épuisement ; dans l'ordre
        # du journal (id 2 > id 1), c'est la réouverture qui fait foi.
        notes = [{"id": 2, "note": "roadmap: rouverte ameesh", "actor": "pilote"},
                 {"id": 1, "note": "roadmap: épuisée ameesh", "actor": "pilote"}]
        self.assertFalse(fd.exhausted(notes, "ameesh"))

    def test_no_note_is_not_exhausted(self):
        self.assertFalse(fd.exhausted([], "ameesh"))
        self.assertIsNone(fd.roadmap_state([], "ameesh"))

    def test_a_sleeping_project_is_found_from_the_pause_reason(self):
        asleep = agent("dev0", operator_paused=True,
                       operator_pause_reason=fd.SLEEP_REASON % "ameesh")
        self.assertEqual(fd.sleeping_projects([asleep]), {"ameesh"})
        other = agent("dev1", operator_paused=True,
                      operator_pause_reason="maintenance")
        self.assertEqual(fd.sleeping_projects([other]), set())


class PassTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("pilote", "dev0", "dev1", "devb0", "devb1", "devb2"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host,
                            cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice'")
        pilots = mock.patch("ameesh.alert_briefs.project_pilots", return_value=["pilote"])
        pilots.start()
        self.addCleanup(pilots.stop)
        self.state: dict = {}
        self.t0 = time.time()

    def listing(self, idle=2, busy=3):
        return team(idle, busy)

    def run_pass(self, offset, listing=None):
        return fd.run(None, self.db, now=self.t0 + offset, state=self.state,
                      listing=listing if listing is not None else self.listing(),
                      canons=[], settings=SETTINGS)

    def split_lots(self):
        return self.db.query("SELECT id FROM work_items WHERE source = %s ORDER BY id",
                             ("file-decoupage:ameesh",))

    def notes(self, lot):
        return [r["note"] for r in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(lot),))]

    def make_split(self):
        self.assertEqual(self.run_pass(0), [])
        out = self.run_pass(1800)
        self.assertEqual([g["kind"] for g in out], ["split"])
        return int(self.split_lots()[0]["id"])

    def asleep(self, *names, ts=1900):
        return [agent(name, operator_paused=True,
                      operator_pause_reason=fd.SLEEP_REASON % "ameesh",
                      operator_pause_by=fd.SLEEP_ACTOR,
                      operator_pause_for="human:alice",
                      operator_pause_ts=float(self.t0 + ts))
                for name in names]

    def test_exhausted_then_reopened_lifts_the_sleepers_in_the_same_pass(self):
        lot = self.make_split()
        work.note(self.db, lot, "roadmap: épuisée ameesh (docs/ROADMAP.md)",
                  actor="pilote")
        out = self.run_pass(1900)
        self.assertEqual([g["kind"] for g in out], ["sleep"])
        for name in ("dev0", "dev1"):
            self.assertTrue(registry.get(self.db, name)["operator_paused"])
        self.assertEqual(len(fd.exhausted_alerts(self.db, self.listing())), 1)

        work.note(self.db, lot, "roadmap: rouverte ameesh (décision owner)",
                  actor="pilote")
        # Aucun agent mesurable : tous les exécutants du projet dorment. La
        # note doit quand même être lue et les dormeurs levés dans cette passe.
        out = self.run_pass(2000, self.asleep("dev0", "dev1"))
        self.assertEqual([g["kind"] for g in out], ["wake"])
        self.assertEqual(sorted(out[0]["agents"]), ["dev0", "dev1"])
        for name in ("dev0", "dev1"):
            self.assertFalse(registry.get(self.db, name)["operator_paused"])
        lifted = self.db.query(
            "SELECT actor, principal, reason FROM operator_pause_events"
            " WHERE agent = 'dev0' AND paused = false ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((lifted["actor"], lifted["principal"]),
                         (fd.SLEEP_ACTOR, "human:alice"))
        self.assertIn("roadmap: rouverte ameesh", lifted["reason"])
        # Le geste est journalisé sur le lot de découpage : auteur, note, levés.
        journal = [note for note in self.notes(lot)
                   if note.startswith(fd.REOPEN_MARK + " ameesh")]
        self.assertEqual(len(journal), 1)
        self.assertIn("actor pilote", journal[0])
        self.assertIn("roadmap: rouverte ameesh", journal[0])
        self.assertIn("dev0, dev1", journal[0])
        # L'alerte suit la dernière note : la réouverture la retire.
        self.assertEqual(fd.exhausted_alerts(self.db, self.listing()), [])

    def test_the_reopening_journal_is_written_once(self):
        lot = self.make_split()
        work.note(self.db, lot, "roadmap: épuisée ameesh", actor="pilote")
        self.assertEqual([g["kind"] for g in self.run_pass(1900)], ["sleep"])
        work.note(self.db, lot, "roadmap: rouverte ameesh", actor="pilote")
        listing = self.asleep("dev0", "dev1")
        self.assertEqual([g["kind"] for g in self.run_pass(2000, listing)], ["wake"])
        self.assertEqual(self.run_pass(2100, listing), [])
        journal = [note for note in self.notes(lot)
                   if note.startswith(fd.REOPEN_MARK + " ameesh")]
        self.assertEqual(len(journal), 1)

    def test_reopened_then_exhausted_sleeps_again(self):
        lot = self.make_split()
        work.note(self.db, lot, "roadmap: rouverte ameesh", actor="pilote")
        out = self.run_pass(1900)
        self.assertNotIn("sleep", [g["kind"] for g in out])
        for name in ("dev0", "dev1"):
            self.assertFalse(registry.get(self.db, name)["operator_paused"])

        work.note(self.db, lot, "roadmap: épuisée ameesh", actor="pilote")
        out = self.run_pass(2000)
        self.assertEqual([g["kind"] for g in out], ["sleep"])
        for name in ("dev0", "dev1"):
            self.assertTrue(registry.get(self.db, name)["operator_paused"])
        self.assertEqual(len(fd.exhausted_alerts(self.db, self.listing())), 1)


if __name__ == "__main__":
    unittest.main()
