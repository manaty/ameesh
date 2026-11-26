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

    NOW = 1_800_000_000.0

    def _hold(self, note):
        return {"agent": {"note": note, "by": "agent"}}

    def test_owner_and_bureau_holds_gate(self):
        for who in ("owner", "avatar"):
            with self.subTest(who=who):
                gate = planned_waits.human_gate(
                    7, self._hold("hold: attente %s : login web" % who), (),
                    now=self.NOW)
                self.assertEqual(gate["executor"], who)
                self.assertEqual(gate["gesture"], "login web")
                self.assertIsNone(gate["until"])

    def test_the_armed_wake_is_not_the_deadline(self):
        gate = planned_waits.human_gate(
            7, self._hold("hold: attente owner : séance DH-6 "
                          "[réveil programmé: 2020-01-01 10:00 UTC]"), (),
            now=self.NOW)
        self.assertIsNotNone(gate, "le réveil armé par la note est celui qu'on reporte")

    def test_a_past_explicit_due_releases(self):
        for note in ("hold: attente owner (due 2020-01-01 10:00 UTC) : login",
                     "hold: attente owner : login (due 2020-01-01)"):
            with self.subTest(note=note):
                self.assertIsNone(planned_waits.human_gate(
                    7, self._hold(note), (), now=self.NOW))

    def test_a_future_due_bounds_the_gate(self):
        gate = planned_waits.human_gate(
            7, self._hold("hold: attente avatar : relais (due 2099-01-01 10:00 UTC)"),
            (), now=self.NOW)
        self.assertGreater(gate["until"], self.NOW)

    def test_unreadable_due_or_other_waits_do_not_gate(self):
        for note in ("hold: attente owner : login (due demain matin)",
                     "hold: attente externe : revue Apple",
                     "hold: séance owner",
                     "hold: attendre le paquet 451"):
            with self.subTest(note=note):
                self.assertIsNone(planned_waits.human_gate(
                    7, self._hold(note), (), now=self.NOW))

    def test_open_decision_gates_until_its_deadline(self):
        request = {"id": 233821, "lot": 7, "owner": "human:o",
                   "due_ts": self.NOW + 3600}
        gate = planned_waits.human_gate(7, {}, [request], now=self.NOW)
        self.assertEqual(gate["what"], "decision")
        self.assertEqual(gate["gesture"], "decision #233821")
        self.assertIsNone(planned_waits.human_gate(8, {}, [request], now=self.NOW))
        self.assertIsNone(planned_waits.human_gate(
            7, {}, [dict(request, due_ts=self.NOW - 1)], now=self.NOW),
            "une décision échue : l'humain est en retard, le réveil part")


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
