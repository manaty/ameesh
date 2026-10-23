# SPDX-License-Identifier: AGPL-3.0-only
"""#2954 : `ameesh work set-type` — retyper un lot avec une ligne de journal.

Un lot mal typé à sa création (cas de #2988, `bug` au lieu d'`evolution`) se
corrige sans le recréer. Le geste est réservé aux correcteurs (humain,
orchestrateur ou agent de conception), journalé, idempotent, et conditionné au
type lu (deux corrections concurrentes ne s'écrasent pas). Le type est la
source du libellé du ticket miroir et du tableau de référence des estimations :
aucune copie à mettre à jour.
"""
from __future__ import annotations

from ameesh import db as db_mod, storage, work
from .test_l118_assignments import _Base


class WorkSetTypeTest(_Base):

    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1"):
            self.agent(name)

    def lot(self, **kw):
        kw.setdefault("title", "Retypage")
        kw.setdefault("type", "bug")
        return work.add(self.db, **kw)

    def test_human_correction_is_journaled_and_idempotent(self):
        item = self.lot()
        proc = self.mesh("work", "set-type", str(item["id"]), "evolution",
                         "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, item["id"])["type"], "evolution")
        events = work.events(self.db, item["id"])
        self.assertEqual(events[0]["actor"], "human:owner")
        self.assertEqual(events[0]["note"], "type 'bug' → 'evolution'")
        proc = self.mesh("work", "set-type", str(item["id"]), "evolution",
                         "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.events(self.db, item["id"]), events)

    def test_improvement_basket_can_become_an_evolution(self):
        # un panier est créé par la file (`improvement`), pas par `work add` :
        # on pose le type réel du panier avant la correction
        item = self.lot()
        self.db.execute("UPDATE work_items SET type = 'improvement',"
                        " expected_value = 'panier', value_score = 10 WHERE id = %s",
                        (item["id"],))
        proc = self.mesh("work", "set-type", str(item["id"]), "evolution",
                         "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, item["id"])["type"], "evolution")

    def test_bound_worker_cannot_correct_or_impersonate(self):
        item = self.lot()
        events = work.events(self.db, item["id"])
        env = self.env(AGENT_MAIL_NAME="dev1", AMEESH_ALERT_ORCHESTRATORS="orch")
        for actor in (None, "human:owner", "orch"):
            with self.subTest(actor=actor):
                args = ("--actor", actor) if actor else ()
                proc = self.mesh("work", "set-type", str(item["id"]), "evolution",
                                 *args, env=env)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("refused", proc.stderr)
                self.assertEqual(work.get(self.db, item["id"])["type"], "bug")
                self.assertEqual(work.events(self.db, item["id"]), events)

    def test_bound_orchestrator_corrects_as_itself(self):
        item = self.lot(type="evolution")
        env = self.env(AGENT_MAIL_NAME="orch", AMEESH_ALERT_ORCHESTRATORS="orch")
        proc = self.mesh("work", "set-type", str(item["id"]), "bug", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        event = work.events(self.db, item["id"])[0]
        self.assertEqual((event["actor"], event["note"]),
                         ("orch", "type 'evolution' → 'bug'"))

    def test_unknown_type_is_refused_by_the_command_and_the_api(self):
        item = self.lot()
        proc = self.mesh("work", "set-type", str(item["id"]), "feature",
                         "--actor", "human:owner")
        self.assertNotEqual(proc.returncode, 0)
        with self.assertRaises(work.WorkError):
            work.set_type(self.db, item["id"], "feature", actor="human:owner")
        self.assertEqual(work.get(self.db, item["id"])["type"], "bug")

    def test_improvement_is_not_a_target(self):
        # la contrainte 0049 exige expected_value et value_score ; on sort
        # d'un panier par set-type, on n'y entre pas
        item = self.lot()
        with self.assertRaises(work.WorkError) as raised:
            work.set_type(self.db, item["id"], "improvement", actor="human:owner")
        self.assertIn("improvement", str(raised.exception))
        self.assertEqual(work.get(self.db, item["id"])["type"], "bug")

    def test_storage_refuses_a_stale_compare_and_set(self):
        item = self.lot(type="bug")
        api = storage.of(self.db).work
        self.assertIsNone(api.set_type(item["id"], "evolution",
                                       current="improvement", actor="orch"))
        self.assertEqual(work.get(self.db, item["id"])["type"], "bug")
        updated = api.set_type(item["id"], "evolution", current="bug", actor="orch")
        self.assertEqual(updated["type"], "evolution")

    def test_missing_lot_is_refused(self):
        proc = self.mesh("work", "set-type", "999999", "evolution",
                         "--actor", "human:owner")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not found", proc.stderr)
