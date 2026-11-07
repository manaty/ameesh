# SPDX-License-Identifier: AGPL-3.0-only
"""#6134 — un seul tour actif par agent, et un réveil armé par un hold levé ne part pas.

Constat (mty-nxl-impl-02, 07/11) : deux sessions du même agent ont agi sur le
même lot après un unhold. Les tours étaient séquentiels ; le réveil programmé
armé par la note `hold:` a réveillé la session de l'autre lot après la levée.

Deux gardes :

* le bail garantissait un seul tour actif, sauf lorsqu'un second processus du
  **même identifiant d'exécuteur** réclamait un agent dont le tour était encore
  en cours : il ne voyait la perte de son epoch qu'au battement suivant
  (`lease_ttl / 3`, 100 s par défaut), et deux tours pouvaient se chevaucher et
  écrire la même branche ;
* un réveil armé par une note `hold:` appartient au plan de cette retenue : si
  elle est levée avant l'échéance, l'exécuteur annule le réveil (ligne
  `wake: cancelled …`) au lieu de réveiller une session sur un plan abandonné.
  L'annulation est un CAS (échéance, armement, claim **vivant**) : un claim
  expiré est une livraison abandonnée et ne bloque pas l'effacement, sans quoi
  le réveil serait relu et refusé à chaque passage, sans fin (#6134 B/P2).
"""
from __future__ import annotations

import os
import time
import unittest

from ameesh import registry, storage, work
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase

_FORCE = "test: two lots for one agent (#6134)"


class _Base(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE agent_lot_sessions, work_items, agent_registry CASCADE")

    def bail(self, name: str):
        runner = Runner(self.cfg, self.db, once=True)
        self.register(name, "claude", cwd=self._cwd(name))
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        return runner, lease

    def _cwd(self, name: str) -> str:
        path = os.path.join(self.tmp, "work", name)
        os.makedirs(path, exist_ok=True)
        return path

    def _worker(self, name: str) -> AgentWorker:
        runner, lease = self.bail(name)
        return AgentWorker(runner, registry.get(self.db, name), lease)


class SameOwnerTurnTest(_Base):
    """Le même identifiant d'exécuteur ne reprend pas un tour en cours."""

    def test_a_running_turn_is_not_reclaimed_by_the_same_runner(self):
        runner, lease = self.bail("a1")
        owner = runner.runner_id
        epoch = int(lease["lease_epoch"])
        self.assertTrue(registry.begin_turn(self.db, "a1", owner, epoch, "turn"))
        self.assertEqual(registry.get(self.db, "a1")["status"], "running")

        self.assertIsNone(
            registry.claim(self.db, "a1", owner, 3600),
            "un second processus du même exécuteur ne prend pas un tour en cours")
        self.assertIsNone(
            registry.claim(self.db, "a1", "another-runner", 3600),
            "un autre exécuteur ne prend pas un bail vivant")

        self.assertTrue(registry.end_turn(self.db, "a1", owner, epoch))
        self.assertIsNotNone(
            registry.claim(self.db, "a1", owner, 3600),
            "entre deux tours, la reprise immédiate du même exécuteur reste permise")

    def test_an_idle_live_lease_is_reclaimed_by_the_same_runner(self):
        """Une reprise après redémarrage entre deux tours reste immédiate."""
        runner, lease = self.bail("a2")
        second = registry.claim(self.db, "a2", runner.runner_id, 3600)
        self.assertIsNotNone(second)
        self.assertEqual(int(second["lease_epoch"]), int(lease["lease_epoch"]) + 1)

    def test_the_runner_names_the_refusal(self):
        runner, lease = self.bail("a3")
        self.assertTrue(registry.begin_turn(self.db, "a3", runner.runner_id,
                                            int(lease["lease_epoch"]), "turn"))
        self.assertTrue(runner._same_owner_turn_in_progress("a3"))
        self.assertFalse(runner._same_owner_turn_in_progress("a2"))


class WakeAfterLiftTest(_Base):
    """Un réveil armé par un hold ne survit pas à la levée de ce hold."""

    def _lot(self, worker: AgentWorker) -> int:
        lot = work.add(self.db, title="lot", assignee=worker.name,
                       force=_FORCE)["id"]
        work.move(self.db, lot, "build")
        return lot

    def _notes(self, lot: int) -> list[str]:
        return [str(event.get("note") or "") for event in work.events(self.db, lot)]

    def test_a_wake_armed_by_a_lifted_hold_is_cancelled(self):
        worker = self._worker("w1")
        try:
            lot = self._lot(worker)
            now = time.time()
            work.note(self.db, lot, "hold: attendre le paquet 451", actor="w1",
                      wake_ts=now - 1)
            work.note(self.db, lot, "unhold: le paquet 451 est fusionné",
                      actor="human:owner")
            worker._scheduled_wake_checked = float("-inf")
            self.assertIsNone(worker._scheduled_wake_spec())
            self.assertIsNone(work.get(self.db, lot)["wake_ts"],
                              "l'échéance abandonnée est retirée, pas réclamée en boucle")
            self.assertTrue(any(note.startswith("wake: cancelled")
                                for note in self._notes(lot)),
                            "l'annulation est nommée au journal du lot")
        finally:
            worker.watchdog_stop.set()

    def test_a_wake_armed_by_a_schedule_survives_a_lift(self):
        """Seul un réveil armé par la note de retenue suit la retenue."""
        worker = self._worker("w2")
        try:
            lot = self._lot(worker)
            work.schedule_wake(self.db, lot, time.time() - 1,
                               text="reprise de veille", actor="w2")
            work.note(self.db, lot, "unhold: rien à lever", actor="human:owner")
            worker._scheduled_wake_checked = float("-inf")
            spec = worker._scheduled_wake_spec()
            self.assertIsNotNone(spec, "un rendez-vous explicite n'est pas annulé")
            self.assertEqual(spec["lot"], str(lot))
            worker._forget_wake_claim()
        finally:
            worker.watchdog_stop.set()

    def test_a_concurrent_rearm_is_not_cancelled(self):
        """B/P2 : l'annulation est un CAS ; un réarmement concurrent survit."""
        worker = self._worker("w4")
        try:
            lot = self._lot(worker)
            old = time.time() - 1
            work.note(self.db, lot, "hold: attendre", actor="w4", wake_ts=old)
            work.note(self.db, lot, "unhold: lever", actor="human:owner")
            reason, arm_id = worker._wake_abandoned_by_lift(lot)
            self.assertTrue(reason)
            self.assertIsNotNone(arm_id)
            new_ts = time.time() + 600
            work.schedule_wake(self.db, lot, new_ts, text="reprise concurrente",
                               actor="w4")
            before = work.get(self.db, lot)["wake_ts"]
            self.assertIsNone(
                work.schedule_wake(self.db, lot, None, text=reason, actor="ameesh",
                                   expect=old, arm_id=arm_id),
                "le réarmement concurrent n'est pas écrasé")
            self.assertAlmostEqual(work.get(self.db, lot)["wake_ts"], before, delta=2)
            self.assertFalse(any(note.startswith("wake: cancelled")
                                 for note in self._notes(lot)))
        finally:
            worker.watchdog_stop.set()

    def test_an_expired_claim_does_not_block_the_cancellation(self):
        """B/P2 : un claim expiré est une livraison abandonnée, pas un détenteur.

        Réveil réclamé puis lancement abandonné (ou runner interrompu), hold
        levé : sans la distinction vivant/expiré, `due_wakes` rend le réveil,
        l'annulation refuse d'écrire (claim non nul) et la boucle se répète
        sans fin."""
        worker = self._worker("w5")
        try:
            lot = self._lot(worker)
            old = time.time() - 1
            work.note(self.db, lot, "hold: attendre le paquet 452", actor="w5",
                      wake_ts=old)
            work.note(self.db, lot, "unhold: le paquet 452 est fusionné",
                      actor="human:owner")
            self.assertIsNotNone(
                storage.of(self.db).lot_sessions.claim_wake(
                    worker.name, lot, time.time(), worker.WAKE_CLAIM_TTL),
                "le réveil est d'abord réclamé par une livraison")
            self.db.execute(
                "UPDATE work_items SET wake_claim_at ="
                " clock_timestamp() - make_interval(secs => %s) WHERE id = %s",
                (worker.WAKE_CLAIM_TTL * 2.5, lot))
            worker._scheduled_wake_checked = float("-inf")
            self.assertIsNone(worker._scheduled_wake_spec())
            self.assertIsNone(work.get(self.db, lot)["wake_ts"],
                              "le claim expiré n'empêche pas l'annulation")
            self.assertTrue(any(note.startswith("wake: cancelled")
                                for note in self._notes(lot)),
                            "l'annulation est nommée au journal du lot")
        finally:
            worker.watchdog_stop.set()

    def test_a_live_claim_still_blocks_the_cancellation(self):
        """B/P2 : entre la lecture et le CAS, une livraison vivante est gardée."""
        worker = self._worker("w6")
        try:
            lot = self._lot(worker)
            old = time.time() - 1
            work.note(self.db, lot, "hold: attendre", actor="w6", wake_ts=old)
            work.note(self.db, lot, "unhold: lever", actor="human:owner")
            reason, arm_id = worker._wake_abandoned_by_lift(lot)
            self.assertTrue(reason)
            self.assertIsNotNone(
                storage.of(self.db).lot_sessions.claim_wake(
                    worker.name, lot, time.time(), worker.WAKE_CLAIM_TTL),
                "un autre passage réclame le réveil entre la lecture et le CAS")
            self.assertIsNone(
                work.schedule_wake(self.db, lot, None, text=reason, actor="ameesh",
                                   expect=old, arm_id=arm_id,
                                   claim_ttl=worker.WAKE_CLAIM_TTL),
                "le claim vivant n'est pas écrasé par l'annulation")
            self.assertAlmostEqual(work.get(self.db, lot)["wake_ts"], old, delta=2,
                                   msg="l'échéance réclamée reste armée")
            self.assertFalse(any(note.startswith("wake: cancelled")
                                 for note in self._notes(lot)))
        finally:
            worker.watchdog_stop.set()

    def test_a_wake_still_under_its_hold_is_delivered(self):
        worker = self._worker("w3")
        try:
            lot = self._lot(worker)
            work.note(self.db, lot, "hold: séance owner", actor="w3",
                      wake_ts=time.time() - 1)
            worker._scheduled_wake_checked = float("-inf")
            spec = worker._scheduled_wake_spec()
            self.assertIsNotNone(spec, "une retenue encore active réveille à l'échéance")
            self.assertEqual(spec["lot"], str(lot))
            worker._forget_wake_claim()
        finally:
            worker.watchdog_stop.set()


if __name__ == "__main__":
    unittest.main()
