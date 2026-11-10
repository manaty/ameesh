# SPDX-License-Identifier: AGPL-3.0-only
"""#6734 — un réveil programmé lève le sommeil « feuille de route épuisée ».

Constat (dev3, 10/11) : le sommeil automatique est une pause d'opérateur, et
l'exécuteur ne délivre aucun tour à un agent en pause. Le réveil de #6532 dû à
12:45 n'est parti qu'à la levée de 13:59, celui de 14:45 qu'à celle de 17:06 ;
le journal « deferred: shared database admission » n'était qu'un témoin.

Témoins du lot :

* un réveil dû (ou dû dans `WAKE_LEAD_S`) sur un lot ouvert de l'agent est un
  travail engagé (`owed_work`), un réveil lointain ne l'est pas ;
* l'agent qui l'attend n'est pas endormi ;
* le dormeur est levé quand l'échéance approche, motif au journal des pauses.
"""
from __future__ import annotations

import time
from unittest import mock

from ameesh import file_decoupage as fd
from ameesh import mechanics, registry, work

from .support import PgTestCase
from .test_4610_sleep_work_mail import agent

SETTINGS = mechanics.QueueFill(repos_part=0.20, repos_s=1800, wake_mail_s=7200)


class SleepScheduledWakeTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("pilote", "dev0", "dev1"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host,
                            cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice',"
                        " team = 'ameesh'")
        pilots = mock.patch("ameesh.alert_briefs.project_pilots", return_value=["pilote"])
        pilots.start()
        self.addCleanup(pilots.stop)
        self.state: dict = {}
        self.t0 = time.time()

    def run_pass(self, offset, listing=None):
        listing = listing if listing is not None else [agent("dev0"), agent("dev1")]
        return fd.run(None, self.db, now=self.t0 + offset, state=self.state,
                      listing=listing, canons=[], settings=SETTINGS)

    def exhausted_split(self):
        self.run_pass(0)
        self.run_pass(1800)
        split = int(self.db.query("SELECT id FROM work_items WHERE source = %s",
                                  ("file-decoupage:ameesh",))[0]["id"])
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        return split

    def notes(self, lot):
        return [r["note"] for r in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(lot),))]

    def sleep_out(self, offset=1900):
        out = self.run_pass(offset)
        sleep = next(g for g in out if g["kind"] == "sleep")
        return {row["agent"]: row for row in sleep["agents"]}

    def waiting_lot(self, assignee, wake_ts):
        """Un lot validé (non actionnable) de `assignee`, réveil armé à `wake_ts`."""
        lot = int(work.add(self.db, title="rendez-vous", app="ameesh",
                           actor="pilote")["id"])
        work.assign(self.db, lot, assignee, actor="pilote")
        self.db.execute("UPDATE work_items SET state = 'validated',"
                        " wake_at = to_timestamp(%s) WHERE id = %s",
                        (float(wake_ts), lot))
        return lot

    def open_lots(self):
        return self.db.query("SELECT id, assignee,"
                             " extract(epoch from wake_at)::float8 AS wake_ts"
                             " FROM work_items WHERE wake_at IS NOT NULL")

    def test_a_due_wake_is_owed_work_and_a_far_one_is_not(self):
        due = self.waiting_lot("dev1", self.t0 + fd.WAKE_LEAD_S - 5)
        self.waiting_lot("dev0", self.t0 + 3600)
        owed, error = fd.owed_work(self.db, ["dev0", "dev1"], self.open_lots(),
                                   now=self.t0)
        self.assertIsNone(error)
        self.assertEqual(list(owed), ["dev1"])
        self.assertTrue(owed["dev1"].startswith("scheduled wake due on #%d (" % due))
        self.assertTrue(owed["dev1"].endswith(" UTC)"))

    def test_an_agent_awaiting_its_wake_is_not_slept(self):
        split = self.exhausted_split()
        lot = self.waiting_lot("dev1", self.t0 + 1900 + 30)
        by_agent = self.sleep_out()
        self.assertFalse(by_agent["dev1"]["slept"])
        self.assertTrue(by_agent["dev1"]["why"].startswith(
            "scheduled wake due on #%d" % lot))
        self.assertTrue(by_agent["dev0"]["slept"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertTrue(any(note.startswith("sleep exempted: dev1: scheduled wake due")
                            for note in self.notes(split)))

    def test_a_sleeper_is_woken_before_its_scheduled_wake(self):
        self.exhausted_split()
        lot = self.waiting_lot("dev1", self.t0 + 3600)
        by_agent = self.sleep_out()
        self.assertTrue(by_agent["dev1"]["slept"])

        def listing():
            return [dict(agent(name), **{
                k: registry.get(self.db, name)[k]
                for k in ("operator_paused", "operator_pause_reason",
                          "operator_pause_by", "operator_pause_for")})
                for name in ("dev0", "dev1")]
        # encore loin de l'échéance : le dormeur reste en sommeil
        out = self.run_pass(3600 - fd.WAKE_LEAD_S - 60, listing=listing())
        self.assertEqual([g for g in out if g["kind"] == "wake"], [])
        self.assertTrue(registry.get(self.db, "dev1")["operator_paused"])
        # dans l'avance : levé, motif au journal des pauses
        out = self.run_pass(3600 - fd.WAKE_LEAD_S + 1, listing=listing())
        woken = [g for g in out if g["kind"] == "wake"]
        self.assertEqual([g["agents"] for g in woken], [["dev1"]])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertTrue(registry.get(self.db, "dev0")["operator_paused"])
        event = self.db.query(
            "SELECT actor, reason FROM operator_pause_events WHERE agent = 'dev1'"
            " AND paused = false ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(event["actor"], fd.SLEEP_ACTOR)
        self.assertTrue(event["reason"].startswith(
            "%s scheduled wake due on #%d" % (fd.SLEEP_LIFT_PREFIX, lot)))
