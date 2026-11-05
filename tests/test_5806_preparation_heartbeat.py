# SPDX-License-Identifier: AGPL-3.0-only
"""#5806 : préparation bloquée, horloge de bail avancée par le témoin."""
from __future__ import annotations

import threading
import time
import types
import unittest
from unittest import mock

from ameesh import registry
from ameesh.runner import AgentWorker


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    monotonic = time


class PreparationHeartbeatTest(unittest.TestCase):
    def worker(self, name, clock):
        worker = AgentWorker.__new__(AgentWorker)
        threading.Thread.__init__(worker, name=name, daemon=True)
        worker.runner = types.SimpleNamespace(lease_ttl=90.0, runner_id="runner")
        worker.db = types.SimpleNamespace(cfg=types.SimpleNamespace(backend="mediated"))
        worker.clock = clock
        worker.epoch = 1
        worker.lease_deadline = clock.time() + 90
        worker.lease_held_until = clock.time() + 90
        worker.renew_failures = 0
        worker.renewals = []
        worker._renew_lock = threading.Lock()
        worker._heartbeat_done = None
        worker.heartbeat_thread = None
        worker.watchdog_stop = threading.Event()
        worker.lease_lost = threading.Event()
        worker.stopping = threading.Event()
        worker.wake = threading.Event()
        worker.ensure_watchdog = lambda: None
        worker.ensure_peek = lambda: None
        worker.stop_group_now = mock.Mock()
        # L'horloge du bail est simulée ; les threads ne dorment pas 30 s.
        worker.wait_timeout = lambda cap: min(0.005, cap)
        worker._apply_pending_config = lambda: None
        worker.retry_remaining = lambda: 0
        worker.apply_restart_if_requested = lambda: None
        worker.maybe_rotate = lambda: None
        worker.rotation_stopped = False
        worker.pick = lambda: None
        worker.rotate_for_lot = lambda spec: spec
        worker._forget_wake_claim = lambda: None
        return worker

    def test_two_leases_survive_preparation_longer_than_ttl(self):
        clock = Clock()
        workers = [self.worker(name, clock) for name in ("selecting", "rotating")]
        workers[1].runner = workers[0].runner
        workers[1].db = workers[0].db
        entered = [threading.Event(), threading.Event()]
        unblock = threading.Event()
        condition = threading.Condition()
        renewed = {w.name: clock.time() for w in workers}
        server_deadlines = {w.name: w.lease_deadline for w in workers}
        failures = []

        def prepare(index):
            entered[index].set()
            if not unblock.wait(10):
                raise AssertionError("preparation not released")
            return None

        workers[0].pick = lambda: prepare(0)
        workers[1].maybe_rotate = lambda: prepare(1)

        def renew(db, name, owner, epoch, ttl):
            with condition:
                if clock.time() >= server_deadlines[name]:
                    return None  # même refus qu'un bail expiré au serveur
                server_deadlines[name] = clock.time() + ttl
                return server_deadlines[name]

        def heartbeat_renew(worker):
            result = AgentWorker.renew(worker)
            with condition:
                # Accusé après mise à jour de l'échéance locale, pas seulement
                # après réponse du serveur dans le thread de _renew_call.
                renewed[worker.name] = worker.lease_deadline - worker.runner.lease_ttl
                condition.notify_all()
            return result

        for worker in workers:
            worker.renew = lambda worker=worker: heartbeat_renew(worker)

        def run(worker):
            try:
                worker.process_once(0)
            except BaseException as error:
                failures.append(error)

        threads = [threading.Thread(target=run, args=(w,)) for w in workers]
        with mock.patch.object(registry, "renew", side_effect=renew), \
                mock.patch("ameesh.runner.log_async") as logs:
            try:
                for thread in threads:
                    thread.start()
                for event in entered:
                    self.assertTrue(event.wait(10), "preparation never entered")
                for _ in range(7):  # 210 s bloquées, TTL = 90 s
                    with condition:
                        clock.now += 30
                        self.assertTrue(condition.wait_for(
                            lambda: all(t >= clock.time() for t in renewed.values()), 10),
                            "lease renewal starved during preparation")
                    for worker in workers:
                        self.assertGreater(worker.lease_deadline, clock.time())
                        self.assertFalse(worker.lease_lost.is_set())
                messages = [call.args[0] for call in logs.call_args_list]
                self.assertTrue(any("choosing turn" in m for m in messages))
                self.assertTrue(any("session rotation" in m for m in messages))
            finally:
                unblock.set()
                for thread in threads:
                    thread.join(10)
                for worker in workers:
                    worker.watchdog_stop.set()
                    if worker.heartbeat_thread is not None:
                        worker.heartbeat_thread.join(10)
        self.assertEqual(failures, [])
        for worker in workers:
            worker.stop_group_now.assert_not_called()
            self.assertFalse(worker.heartbeat_thread.is_alive())

    def test_service_and_nested_turn_share_one_heartbeat(self):
        worker = self.worker("service", Clock())
        started = threading.Event()
        stopped = threading.Event()
        scopes = []

        def heartbeat(done):
            scopes.append(done)
            started.set()
            done.wait(10)
            stopped.set()

        def service():
            self.assertTrue(started.wait(10))
            outer = worker.heartbeat_thread
            with worker._lease_heartbeat():  # résumé de rotation imbriqué
                self.assertIs(worker.heartbeat_thread, outer)
            self.assertFalse(stopped.is_set())
            raise RuntimeError("exit")

        worker._heartbeat = heartbeat
        worker._run_under_heartbeat = service
        with self.assertRaisesRegex(RuntimeError, "exit"):
            worker.run()
        self.assertTrue(stopped.wait(10))
        worker.heartbeat_thread.join(10)
        self.assertEqual(len(scopes), 1)


if __name__ == "__main__":
    unittest.main()
