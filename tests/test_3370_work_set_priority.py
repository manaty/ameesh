# SPDX-License-Identifier: AGPL-3.0-only
"""#3370 : `ameesh work set-priority` — priorité d'un lot ouvert, journalisée.

Un lot mal classé (cas #3358, P1 alors que le pilote le juge non urgent) se
corrige sans le recréer, avec un motif obligatoire et une ligne de journal.
Le geste est réservé aux correcteurs (humain, orchestrateur ou agent de
conception), idempotent, et conditionné à la priorité lue : deux corrections
concurrentes ne s'écrasent pas. L'état ouvert est revérifié sous le verrou de
ligne de l'écriture : une transition intercalée (promotion, fusion, fermeture)
ne reçoit pas la priorité. La priorité est lue par les files, les alertes §9
et la projection GitHub ; aucune copie à mettre à jour.
"""
from __future__ import annotations

import json
from unittest import mock

from ameesh import storage, work
from .test_l118_assignments import _Base


class WorkSetPriorityTest(_Base):

    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1"):
            self.agent(name)

    def lot(self, **kw):
        kw.setdefault("title", "Priorité")
        kw.setdefault("type", "evolution")
        return work.add(self.db, **kw)

    def test_human_change_is_journaled_and_idempotent(self):
        item = self.lot(priority=1)
        proc = self.mesh("work", "set-priority", str(item["id"]), "2",
                         "--reason", "staging only, not urgent",
                         "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, item["id"])["priority"], 2)
        events = work.events(self.db, item["id"])
        self.assertEqual(events[0]["actor"], "human:owner")
        self.assertEqual(events[0]["note"],
                         "priority 1 → 2 — staging only, not urgent")
        proc = self.mesh("work", "set-priority", str(item["id"]), "2",
                         "--reason", "staging only, not urgent",
                         "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.events(self.db, item["id"]), events)

    def test_reason_is_required(self):
        item = self.lot(priority=1)
        events = work.events(self.db, item["id"])
        proc = self.mesh("work", "set-priority", str(item["id"]), "2",
                         "--actor", "human:owner")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("reason", proc.stderr)
        self.assertEqual(work.get(self.db, item["id"])["priority"], 1)
        self.assertEqual(work.events(self.db, item["id"]), events)
        with self.assertRaises(work.WorkError) as raised:
            work.set_priority(self.db, item["id"], 2, actor="human:owner")
        self.assertIn("reason", str(raised.exception))

    def test_bound_worker_cannot_correct_or_impersonate(self):
        item = self.lot(priority=1)
        events = work.events(self.db, item["id"])
        env = self.env(AGENT_MAIL_NAME="dev1", AMEESH_ALERT_ORCHESTRATORS="orch")
        for actor in (None, "human:owner", "orch"):
            with self.subTest(actor=actor):
                args = ("--actor", actor) if actor else ()
                proc = self.mesh("work", "set-priority", str(item["id"]), "2",
                                 "--reason", "pas urgent", *args, env=env)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("refused", proc.stderr)
                self.assertEqual(work.get(self.db, item["id"])["priority"], 1)
                self.assertEqual(work.events(self.db, item["id"]), events)

    def test_bound_orchestrator_changes_as_itself(self):
        item = self.lot(priority=1)
        env = self.env(AGENT_MAIL_NAME="orch", AMEESH_ALERT_ORCHESTRATORS="orch")
        proc = self.mesh("work", "set-priority", str(item["id"]), "3",
                         "--reason", "backlog", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        event = work.events(self.db, item["id"])[0]
        self.assertEqual((event["actor"], event["note"]),
                         ("orch", "priority 1 → 3 — backlog"))

    def test_unknown_priority_is_refused_by_the_command_and_the_api(self):
        item = self.lot(priority=1)
        proc = self.mesh("work", "set-priority", str(item["id"]), "4",
                         "--reason", "x", "--actor", "human:owner")
        self.assertNotEqual(proc.returncode, 0)
        with self.assertRaises(work.WorkError):
            work.set_priority(self.db, item["id"], 4, reason="x", actor="human:owner")
        self.assertEqual(work.get(self.db, item["id"])["priority"], 1)

    def test_a_lot_without_priority_gets_one(self):
        item = self.lot()
        self.assertIsNone(work.get(self.db, item["id"])["priority"])
        updated = work.set_priority(self.db, item["id"], 2, reason="triage",
                                    actor="human:owner")
        self.assertEqual(updated["priority"], 2)
        self.assertEqual(work.events(self.db, item["id"])[0]["note"],
                         "priority none → 2 — triage")

    def test_finished_lot_is_refused(self):
        item = self.lot(priority=1)
        self.db.execute("UPDATE work_items SET state = 'promoted' WHERE id = %s",
                        (item["id"],))
        proc = self.mesh("work", "set-priority", str(item["id"]), "2",
                         "--reason", "x", "--actor", "human:owner")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("open lot", proc.stderr)
        with self.assertRaises(work.WorkError):
            work.set_priority(self.db, item["id"], 2, reason="x", actor="human:owner")
        self.assertEqual(work.get(self.db, item["id"])["priority"], 1)

    def test_storage_refuses_a_stale_compare_and_set(self):
        item = self.lot(priority=1)
        api = storage.of(self.db).work
        self.assertIsNone(api.set_priority(item["id"], 2, current=3,
                                           reason="x", actor="orch"))
        self.assertEqual(work.get(self.db, item["id"])["priority"], 1)
        updated = api.set_priority(item["id"], 2, current=1, reason="x", actor="orch")
        self.assertEqual(updated["priority"], 2)

    def test_storage_refuses_a_finished_lot_under_the_lock(self):
        item = self.lot(priority=1)
        api = storage.of(self.db).work
        for state in ("promoted", "merged", "closed"):
            with self.subTest(state=state):
                sql = "UPDATE work_items SET state = %s WHERE id = %s"
                if state == "closed":
                    sql = ("UPDATE work_items SET state = %s,"
                           " close_reason = 'abandoned' WHERE id = %s")
                self.db.execute(sql, (state, item["id"]))
                self.assertIsNone(api.set_priority(
                    item["id"], 2, current=1, reason="x", actor="orch"))
                self.assertEqual(work.get(self.db, item["id"])["priority"], 1)
                self.assertEqual([
                    event for event in work.events(self.db, item["id"])
                    if (event.get("note") or "").startswith("priority ")], [])

    def test_interleaved_transition_is_refused(self):
        # relecture cx2 : la transition passe entre la lecture de work.py (qui
        # voit le lot ouvert) et l'écriture du stockage ; la garde d'état sous
        # FOR NO KEY UPDATE doit refuser la priorité et le journal.
        item = self.lot(priority=1)
        stale = dict(work.get(self.db, item["id"]))
        self.db.execute("UPDATE work_items SET state = 'promoted' WHERE id = %s",
                        (item["id"],))
        real_get = work.get
        calls = []

        def racing_get(db, item_id, *args, **kwargs):
            calls.append(item_id)
            return stale if len(calls) == 1 else real_get(db, item_id, *args, **kwargs)

        with mock.patch.object(work, "get", side_effect=racing_get):
            with self.assertRaises(work.WorkError) as raised:
                work.set_priority(self.db, item["id"], 2, reason="x",
                                  actor="human:owner")
        self.assertIn("open lot", str(raised.exception))
        self.assertEqual(work.get(self.db, item["id"])["priority"], 1)
        self.assertEqual([
            event for event in work.events(self.db, item["id"])
            if (event.get("note") or "").startswith("priority ")], [])

    def test_missing_lot_is_refused(self):
        proc = self.mesh("work", "set-priority", "999999", "2",
                         "--reason", "x", "--actor", "human:owner")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not found", proc.stderr)

    def test_json_output(self):
        item = self.lot(priority=1)
        proc = self.mesh("work", "set-priority", str(item["id"]), "2",
                         "--reason", "x", "--actor", "human:owner", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["priority"], 2)
