# SPDX-License-Identifier: AGPL-3.0-only
"""#8285 — un réveil programmé sur une situation figée est reporté.

Constat (auditeur, nuit du 26/11) : le 25/11, 25 réveils programmés ont
réécrit à froid des sessions de 213 000 à 284 000 jetons pour constater qu'un
geste owner restait ouvert (décision, login, séance), puis réarmer. Tant que la
demande owner ou avatar qui bloque le lot reste ouverte, l'exécuteur reporte le
réveil sans le consommer ; il part à la réponse, à l'échéance de la demande, ou
au plus tard `FROZEN_WAKE_MAX_DEFER_S` après la sienne.
"""
from __future__ import annotations

import time
import unittest

from ameesh import decisions, planned_waits, work
from ameesh.runner import AgentWorker

from .test_6134_single_turn import _Base

_FORCE = "test: frozen wakes (#8285)"


class HumanGateTest(unittest.TestCase):
    """La porte humaine lue sur les retenues et les décisions du lot."""

    def _hold(self, note):
        return {"agent": {"note": note, "by": "agent"}}

    def test_owner_and_avatar_holds_gate(self):
        for who in ("owner", "avatar"):
            with self.subTest(who=who):
                gate = planned_waits.human_gate(
                    7, self._hold("hold: attente %s : login web" % who), ())
                self.assertEqual(gate["executor"], who)
                self.assertEqual(gate["gesture"], "login web")

    def test_a_runtime_route_gates(self):
        """B1 : `bureau` est l'avatar déclaré au canon, pas un nom du cœur."""
        note = "hold: attente bureau : login web"
        self.assertIsNone(planned_waits.human_gate(7, self._hold(note), ()))
        gate = planned_waits.human_gate(7, self._hold(note), (),
                                        executors={"bureau"})
        self.assertEqual(gate["executor"], "avatar")

    def test_a_declared_due_does_not_release(self):
        """B2 : #8078, notes « due 12:00 » et réveil à 12:00 : la demande
        reste ouverte, le report tient jusqu'à la levée (filet à l'appelant)."""
        for note in ("hold: attente owner (due 2020-01-01 10:00 UTC) : login",
                     "hold: attente owner : séance DH-6 "
                     "[réveil programmé: 2020-01-01 10:00 UTC]"):
            with self.subTest(note=note):
                self.assertIsNotNone(planned_waits.human_gate(
                    7, self._hold(note), ()))

    def test_other_waits_do_not_gate(self):
        for note in ("hold: attente externe : revue Apple",
                     "hold: séance owner",
                     "hold: fenêtre : observation du relais",
                     "hold: attendre le paquet 451"):
            with self.subTest(note=note):
                self.assertIsNone(planned_waits.human_gate(7, self._hold(note), ()))

    def test_open_decision_gates_even_past_its_deadline(self):
        request = {"id": 233821, "lot": 7, "owner": "human:o", "due_ts": 1.0}
        gate = planned_waits.human_gate(7, {}, [request])
        self.assertEqual(gate["what"], "decision")
        self.assertEqual(gate["gesture"], "decision #233821")
        self.assertIsNone(planned_waits.human_gate(8, {}, [request]))


class FrozenWakeTest(_Base):
    """Le report à l'exécuteur : échéance gardée, délivrance à la réponse."""

    def _lot(self, worker: AgentWorker) -> int:
        lot = work.add(self.db, title="lot", assignee=worker.name,
                       force=_FORCE)["id"]
        work.move(self.db, lot, "build")
        return lot

    def _spec(self, worker: AgentWorker):
        worker._scheduled_wake_checked = float("-inf")
        worker.__dict__.pop("_frozen_wakes", None)
        return worker._scheduled_wake_spec()

    def test_owner_hold_defers_until_lifted(self):
        worker = self._worker("f1")
        try:
            lot = self._lot(worker)
            due = time.time() - 1
            work.note(self.db, lot, "hold: attente owner : login web", actor="f1")
            work.schedule_wake(self.db, lot, due, text="relire le login", actor="f1")
            self.assertIsNone(self._spec(worker))
            self.assertAlmostEqual(work.get(self.db, lot)["wake_ts"], due, places=3,
                                   msg="reporté, pas consommé : l'échéance reste armée")
            self.assertIsNone(worker._scheduled_wake_spec(),
                              "relu au plus toutes les FROZEN_WAKE_RECHECK_S")
            work.note(self.db, lot, "unhold: login fait", actor="human:owner")
            spec = self._spec(worker)
            self.assertIsNotNone(spec, "la réponse délivre le réveil")
            self.assertEqual(spec["lot"], str(lot))
            worker._forget_wake_claim()
        finally:
            worker.watchdog_stop.set()

    def test_backstop_delivers_after_max_defer(self):
        worker = self._worker("f2")
        try:
            lot = self._lot(worker)
            work.note(self.db, lot, "hold: attente avatar : relais", actor="f2")
            work.schedule_wake(self.db, lot,
                               time.time() - AgentWorker.FROZEN_WAKE_MAX_DEFER_S - 1,
                               text="veille", actor="f2")
            spec = self._spec(worker)
            self.assertIsNotNone(spec, "le filet délivre une demande restée ouverte")
            worker._forget_wake_claim()
        finally:
            worker.watchdog_stop.set()

    def test_open_decision_defers_until_withdrawn(self):
        worker = self._worker("f3")
        try:
            lot = self._lot(worker)
            due = time.time() - 1
            view = decisions.ask(self.cfg, self.db, requester="f3", lot=str(lot),
                                 question="C1 ?", options=["a=oui", "b=non"],
                                 due_ts=time.time() + 3600)
            work.schedule_wake(self.db, lot, due, text="relire C1", actor="f3")
            self.assertIsNone(self._spec(worker))
            self.assertAlmostEqual(work.get(self.db, lot)["wake_ts"], due, places=3)
            decisions.withdraw(self.cfg, self.db, view["id"], requester="f3")
            spec = self._spec(worker)
            self.assertIsNotNone(spec, "la décision close délivre le réveil")
            worker._forget_wake_claim()
        finally:
            worker.watchdog_stop.set()


if __name__ == "__main__":
    unittest.main()
