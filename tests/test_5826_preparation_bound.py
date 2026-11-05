# SPDX-License-Identifier: AGPL-3.0-only
"""#5826 : borne d'une préparation figée, renouvellement explicite sans doublon."""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from ameesh import registry
from ameesh.runner import AgentWorker

from tests.test_5806_preparation_heartbeat import Clock, PreparationHeartbeatTest


class PreparationBoundTest(unittest.TestCase):
    worker = PreparationHeartbeatTest.worker

    def test_stuck_preparation_is_abandoned_and_lease_released(self):
        clock = Clock()
        worker = self.worker("stuck", clock)
        worker.runner.max_preparation = 120.0
        worker.proc = None
        entered, unblock = threading.Event(), threading.Event()
        launched = []

        def pick():
            entered.set()
            unblock.wait(10)
            return {"kind": "consigne"}

        worker.pick = pick
        worker._moved_before_turn = lambda spec: False
        # le vrai tour refuse de lancer bail perdu (garde avant le Popen)
        worker.run_turn = lambda spec: launched.append(worker.lease_lost.is_set()) or True
        server = {"deadline": worker.lease_deadline}

        def renew(db, name, owner, epoch, ttl):
            if clock.time() >= server["deadline"]:
                return None
            server["deadline"] = clock.time() + ttl
            return server["deadline"]

        released = threading.Event()
        thread = threading.Thread(target=worker.process_once, args=(0,))
        with mock.patch.object(registry, "renew", side_effect=renew), \
                mock.patch.object(registry, "release",
                                  side_effect=lambda *a: released.set() or True) as release, \
                mock.patch("ameesh.runner.log_async") as logs:
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                for _ in range(5):  # 150 s > borne de 120 s, bail de 90 s tenu
                    clock.now += 30
                    if released.wait(0.2):
                        break
                self.assertTrue(released.is_set(), "lease never released")
            finally:
                unblock.set()
                thread.join(10)
                worker.watchdog_stop.set()
                worker.heartbeat_thread.join(10)
        self.assertTrue(worker.lease_lost.is_set())
        reason = worker.stop_group_now.call_args.args[0]
        self.assertIn("preparation stuck", reason)
        self.assertIn("choosing turn", reason)
        self.assertNotIn("lease deadline reached", reason)
        release.assert_called_once_with(worker.db, "stuck", "runner", 1)
        self.assertEqual(launched, [True], "the turn must see the lease lost")
        self.assertTrue(any("turn abandoned" in c.args[0] for c in logs.call_args_list))

    def test_bound_ignores_harness_time_inside_a_phase(self):
        clock = Clock()
        worker = self.worker("nested", clock)
        worker._launches = 0
        worker._preparation_phase = None
        with worker._preparing("session rotation"):
            with worker._preparing("turn setup"):
                worker._preparation_phase = None  # harnais du résumé lancé
                worker._launches += 1
                clock.now += 3600  # tour de résumé long
            self.assertEqual(worker._preparation_phase, ("session rotation", clock.now))
        self.assertIsNone(worker._preparation_phase)

    def test_live_harness_is_never_abandoned(self):
        clock = Clock()
        worker = self.worker("live", clock)
        worker.runner.max_preparation = 60.0
        worker.proc = object()
        worker._preparation_phase = ("turn setup", clock.now)
        done = threading.Event()
        worker.renew = lambda: (done.set(), True)[1]
        worker.renew_failures = 0
        clock.now += 600
        with mock.patch.object(registry, "release") as release, \
                mock.patch("ameesh.runner.log_async"):
            thread = threading.Thread(target=worker._heartbeat, args=(done,))
            thread.start()
            thread.join(10)
        release.assert_not_called()
        self.assertFalse(worker.lease_lost.is_set())

    def test_renew_if_due_skips_while_heartbeat_is_on_time(self):
        clock = Clock()
        worker = self.worker("due", clock)
        worker.renew = mock.Mock(return_value=True)
        worker.lease_deadline = clock.now + 80  # renouvelé il y a 10 s
        self.assertTrue(worker.renew_if_due())
        worker.renew.assert_not_called()
        worker.lease_deadline = clock.now + 40  # battement en retard
        self.assertTrue(worker.renew_if_due())
        worker.renew.assert_called_once_with()
        worker.lease_lost.set()
        self.assertFalse(worker.renew_if_due())
        self.assertEqual(worker.renew.call_count, 1)


if __name__ == "__main__":
    unittest.main()
