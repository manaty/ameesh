# SPDX-License-Identifier: AGPL-3.0-only
"""#3256 A — remplir la file : le lot de découpage (spec
docs/design/3256-debit-file.md §2.4).

Témoins : aucun déclenchement sous 20 % ni sous 30 min, aucun quand un lot
démarrable existe, un seul lot de découpage ouvert par projet ; le repli de
l'auditeur après une heure sans lot démarrable créé ; une feuille de route
notée épuisée endort les agents d'exécution au repos (jamais un critique),
lève une alerte à l'owner, et le premier lot démarrable les réveille ; les
paramètres viennent du bloc `file` du canon.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from ameesh import file_decoupage as fd
from ameesh import mechanics, registry, storage, work

from .support import PgTestCase
from .test_l1616_mechanics import MECHANICS

SETTINGS = mechanics.QueueFill(repos_part=0.20, repos_s=1800)


def agent(name, *, idle=True, project="ameesh", **extra):
    row = {"name": name, "mode": "execute", "status": "idle", "lease_live": True,
           "lease_owner": "runner:h1", "unread": 0, "assigned_lot_id": None,
           "active_lots": 0, "actionable_lots": 0 if idle else 1,
           "projects": [project], "responsible": "human:alice",
           # #4779 : profil de relecteur prouvé (capacité `propose`, un rôle).
           "capabilities": ["read", "report-drift", "propose"],
           "roles": ["implementation"]}
    row.update(extra)
    return row


def team(idle: int, busy: int, project="ameesh", prefix="dev"):
    return ([agent("%s%d" % (prefix, i), project=project) for i in range(idle)]
            + [agent("%sb%d" % (prefix, i), idle=False, project=project)
               for i in range(busy)])


def _footprint(db, item, *paths):
    """#4717 : l'empreinte déclarée du lot, comme `work assign` la lit."""
    storage.of(db).work_footprints.replace(int(item["id"]), "manual",
                                           list(paths), actor="test")


class MeasureTest(unittest.TestCase):
    def measures(self, listing, lots=(), waits=None, footprint=None):
        return fd.measure(listing, list(lots), waits or {}, 1000.0, [],
                          configured=False, footprint=footprint)

    def test_critical_external_and_ephemeral_agents_are_not_wakeable(self):
        listing = team(1, 1) + [agent("auditeur"), agent("chat", mode="externe"),
                                agent("probe", ephemeral=True),
                                agent("dormeur", operator_paused=True),
                                agent("retraite", retired_ts=900.0),
                                agent("sans_executeur", lease_live=False)]
        item = self.measures(listing)["ameesh"]
        self.assertEqual(item.wakeable, ["dev0", "devb0"])
        self.assertEqual(item.idle, ["dev0"])

    def test_startable_lots_are_counted_per_project_and_waits_excluded(self):
        lots = [{"id": 1, "state": "intake", "app": "ameesh", "type": "evolution"},
                {"id": 2, "state": "intake", "app": "ameesh", "type": "evolution"},
                {"id": 3, "state": "intake", "app": "nexlink", "type": "evolution"},
                {"id": 4, "state": "intake", "app": "ameesh", "type": "evolution",
                 "assignee": "devb0"}]
        out = self.measures(team(1, 0) + team(1, 0, "nexlink", "nx"), lots,
                            waits={2: {"what": "dependency", "lots": [9]}})
        self.assertEqual(out["ameesh"].startable, [1])
        self.assertEqual(out["nexlink"].startable, [3])

    def test_a_lot_held_by_the_footprint_is_not_startable(self):
        # #4717 : la garde d'empreinte (#3185) s'applique au déclencheur de
        # découpage comme au débit (#4561) : un lot recouvert par un lot en
        # cours n'est pas un lot démarrable offert aux agents au repos.
        lots = [{"id": 1, "state": "intake", "app": "ameesh", "type": "evolution"},
                {"id": 2, "state": "intake", "app": "ameesh", "type": "evolution"}]
        out = self.measures(team(1, 0), lots,
                            footprint={2: [{"id": 9, "state": "build"}]})
        self.assertEqual(out["ameesh"].startable, [1])


class HeldTest(unittest.TestCase):
    def run_held(self, idle, busy, *, since=None, now=4000.0, startable=()):
        item = fd.Measure("ameesh", wakeable=["a%d" % i for i in range(idle + busy)],
                          idle=["a%d" % i for i in range(idle)], startable=list(startable))
        return fd.held({"ameesh": item}, since or {}, now, 0.20, 1800)

    def test_no_trigger_under_twenty_percent(self):
        due, state = self.run_held(1, 9, since={"ameesh": 0.0})   # 10 % < 20 %
        self.assertEqual(due, [])
        self.assertEqual(state, {})

    def test_no_trigger_under_thirty_minutes(self):
        due, state = self.run_held(2, 8, since={"ameesh": 3000.0})   # 1000 s
        self.assertEqual(due, [])
        self.assertEqual(state, {"ameesh": 3000.0})

    def test_no_trigger_when_a_startable_lot_exists(self):
        due, state = self.run_held(10, 0, since={"ameesh": 0.0}, startable=[7])
        self.assertEqual((due, state), ([], {}))

    def test_trigger_at_twenty_percent_for_thirty_minutes(self):
        due, _ = self.run_held(2, 8, since={"ameesh": 4000.0 - 1800})
        self.assertEqual(due, ["ameesh"])

    def test_first_observation_starts_the_clock(self):
        due, state = self.run_held(5, 0)
        self.assertEqual((due, state), ([], {"ameesh": 4000.0}))


class ExhaustedNoteTest(unittest.TestCase):
    def test_the_note_names_its_project(self):
        self.assertTrue(fd.exhausted([{"note": "roadmap: épuisée ameesh (ROADMAP.md)"}],
                                     "ameesh"))
        self.assertFalse(fd.exhausted([{"note": "roadmap: épuisée nexlink"}], "ameesh"))
        self.assertFalse(fd.exhausted([{"note": "la roadmap: épuisée ameesh"}], "ameesh"))


class MechanicsBlockTest(unittest.TestCase):
    BASE = MECHANICS

    def test_the_file_block_compiles(self):
        doc = mechanics.compile_document(self.BASE + "file:\n  repos_part: 0.20\n"
                                         "  repos_s: 1800\n")
        self.assertEqual(doc.queue_fill, SETTINGS)
        self.assertEqual(doc.to_dict()["file"], {"repos_part": 0.2, "repos_s": 1800})
        self.assertEqual(doc.queue_fill.mail_window(), 1800.0)

    def test_the_mail_window_is_its_own_key_and_defaults_to_repos_s(self):
        # #4610 : la fenêtre du courrier récent se règle à part ; sans clé,
        # elle vaut `repos_s` (le bloc `file` existant garde son sens).
        doc = mechanics.compile_document(self.BASE + "file:\n  repos_part: 0.20\n"
                                         "  repos_s: 1800\n  wake_mail_s: 3600\n")
        self.assertEqual(doc.queue_fill.wake_mail_s, 3600)
        self.assertEqual(doc.queue_fill.mail_window(), 3600.0)
        self.assertEqual(doc.to_dict()["file"],
                         {"repos_part": 0.2, "repos_s": 1800, "wake_mail_s": 3600})

    def test_absent_block_means_no_trigger(self):
        self.assertIsNone(mechanics.compile_document(self.BASE).queue_fill)
        self.assertEqual(fd.run(None, None, settings=None), [])

    def test_bad_values_are_refused(self):
        for block in ("file:\n  repos_part: 0\n  repos_s: 1800\n",
                      "file:\n  repos_part: 1.5\n  repos_s: 1800\n",
                      "file:\n  repos_part: 0.2\n",
                      "file:\n  repos_part: 0.2\n  repos_s: 1800\n  wake_mail_s: 0\n",
                      "file:\n  repos_part: 0.2\n  repos_s: 1800\n  other: 1\n"):
            with self.assertRaises(mechanics.MechanicsError, msg=block):
                mechanics.compile_document(self.BASE + block)


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
        return self.db.query("SELECT id, assignee, priority, type, title, body, state"
                             " FROM work_items WHERE source = %s ORDER BY id",
                             ("file-decoupage:ameesh",))

    def notes(self, lot):
        return [r["note"] for r in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(lot),))]

    def test_one_split_lot_assigned_to_the_pilot_and_never_a_second(self):
        self.assertEqual(self.run_pass(0), [])
        self.assertEqual(self.split_lots(), [])
        out = self.run_pass(1800)
        self.assertEqual([g["kind"] for g in out], ["split"])
        rows = self.split_lots()
        self.assertEqual(len(rows), 1)
        lot = rows[0]
        self.assertEqual((lot["assignee"], lot["priority"], lot["type"]),
                         ("pilote", 1, "evolution"))
        self.assertIn("ameesh", lot["title"])
        self.assertIn("2 agent(s)", lot["title"])
        self.assertIn("dev0, dev1", lot["body"])
        self.assertEqual(self.run_pass(2400), [])
        self.assertEqual(len(self.split_lots()), 1)

    def test_no_split_when_a_startable_lot_exists(self):
        work.add(self.db, title="lot prêt", app="ameesh", actor="test")
        self.run_pass(0)
        self.assertEqual(self.run_pass(3600), [])
        self.assertEqual(self.split_lots(), [])

    def test_a_covered_lot_does_not_hold_the_split(self):
        # #4717, témoin de bout en bout : un lot d'intake recouvert par un lot
        # en cours n'est pas démarrable ; le projet n'a donc aucun lot
        # démarrable et le découpage se déclenche comme si la file était vide.
        occupe = work.add(self.db, title="occupe", app="ameesh", actor="test")
        self.db.execute("UPDATE work_items SET state = 'build', assignee = 'devb0'"
                        " WHERE id = %s", (occupe["id"],))
        recouvert = work.add(self.db, title="recouvert", app="ameesh", actor="test")
        _footprint(self.db, occupe, "src/ameesh/x.py")
        _footprint(self.db, recouvert, "src/ameesh/x.py")
        self.run_pass(0)
        out = self.run_pass(1800)
        self.assertEqual([g["kind"] for g in out], ["split"])
        self.assertEqual(len(self.split_lots()), 1)

    def test_no_split_under_twenty_percent(self):
        listing = team(1, 9)
        self.run_pass(0, listing)
        self.assertEqual(self.run_pass(3600, listing), [])
        self.assertEqual(self.split_lots(), [])

    def test_fallback_mails_the_auditor_once_after_an_hour(self):
        self.run_pass(0)
        self.run_pass(1800)
        lot = int(self.split_lots()[0]["id"])
        self.assertEqual(self.run_pass(1800 + 1200), [])
        out = self.run_pass(1800 + 3700)
        self.assertEqual([g["kind"] for g in out], ["fallback"])
        mails = self.db.query("SELECT recipient FROM agent_mailbox WHERE work_item_id = %s",
                              (str(lot),))
        self.assertEqual([m["recipient"] for m in mails], ["auditeur"])
        self.assertTrue(any(n.startswith(fd.FALLBACK_MARK) for n in self.notes(lot)))
        self.assertEqual(self.run_pass(1800 + 4000), [])

    def test_no_fallback_when_a_startable_lot_was_created(self):
        self.run_pass(0)
        self.run_pass(1800)
        made = work.add(self.db, title="découpé", app="ameesh", actor="pilote")
        work.assign(self.db, int(made["id"]), "devb0", actor="pilote")
        out = self.run_pass(1800 + 3700)
        self.assertNotIn("fallback", [g["kind"] for g in out])

    def test_a_lot_created_already_assigned_is_not_a_startable_arrival(self):
        self.run_pass(0)
        self.run_pass(1800)
        registry.upsert(self.db, "devb9", harness="deepseek", host=self.cfg.host, cwd=self.tmp)
        with mock.patch("ameesh.pilot_gesture.gate", return_value=None):
            work.add(self.db, title="déjà confié", app="ameesh", actor="pilote",
                     assignee="devb9")
        out = self.run_pass(1800 + 3700)
        self.assertEqual([g["kind"] for g in out], ["fallback"])

    def test_exhausted_roadmap_sleeps_idle_executors_alerts_and_wakes(self):
        self.run_pass(0)
        self.run_pass(1800)
        lot = int(self.split_lots()[0]["id"])
        work.note(self.db, lot, "roadmap: épuisée ameesh (docs/ROADMAP.md)", actor="pilote")
        listing = self.listing() + [agent("auditeur")]
        out = self.run_pass(1900, listing)
        self.assertEqual([g["kind"] for g in out], ["sleep"])
        self.assertEqual(sorted(a["agent"] for a in out[0]["agents"] if a["slept"]),
                         ["dev0", "dev1"])
        for name in ("dev0", "dev1"):
            row = registry.get(self.db, name)
            self.assertTrue(row["operator_paused"])
            self.assertEqual(row["operator_pause_reason"], fd.SLEEP_REASON % "ameesh")
        self.assertFalse(registry.get(self.db, "devb0")["operator_paused"])
        alerts = fd.exhausted_alerts(self.db, listing)
        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0]["type"], alerts[0]["lot"], alerts[0]["responsible"]),
                         ("roadmap_exhausted", lot, "human:alice"))
        # le premier lot démarrable réveille les dormeurs
        work.add(self.db, title="nouvelle étape", app="ameesh", actor="owner")
        asleep = [dict(registry.get(self.db, n), projects=["ameesh"], mode="execute")
                  for n in ("dev0", "dev1")]
        out = self.run_pass(2000, asleep + team(0, 3))
        woken = [g for g in out if g["kind"] == "wake"]
        self.assertEqual(woken[0]["agents"], ["dev0", "dev1"])
        self.assertFalse(registry.get(self.db, "dev0")["operator_paused"])


if __name__ == "__main__":
    unittest.main()
