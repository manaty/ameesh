# SPDX-License-Identifier: AGPL-3.0-only
"""#2446 : un seul hôte exécute le service de train, sous bail exclusif."""
from __future__ import annotations

import contextlib
import threading
import types
import unittest
from unittest import mock

from ameesh import train, train_runner
from ameesh.runner import Runner
from .support import PgTestCase


class Ops:
    def __init__(self):
        self.locks = {}
        self.calls = []

    def land_lock(self, key, owner, ttl, *, epoch=None):
        self.calls.append(("lock", key, owner, ttl, epoch))
        if epoch is not None:
            return epoch if self.locks.get(key) == (owner, epoch) else None
        if key in self.locks:
            return None
        self.locks[key] = (owner, 1)
        return 1

    def land_unlock(self, key, owner, epoch):
        self.calls.append(("unlock", key, owner, epoch))
        if self.locks.get(key) == (owner, epoch):
            del self.locks[key]
            return True
        return False


class HostLeaseTest(unittest.TestCase):
    def setUp(self):
        self.ops = Ops()
        self.storage = mock.patch("ameesh.train_runner.storage.of",
                                  return_value=types.SimpleNamespace(work=self.ops))
        self.storage.start()
        self.addCleanup(self.storage.stop)
        self.cfg = types.SimpleNamespace(runner="runner-a@host-a", host="host-a")

    def test_one_lease_fences_two_executors_and_releases_by_epoch(self):
        first = train_runner.HostLease(self.cfg, ttl=900, retry=60)
        second = train_runner.HostLease(
            types.SimpleNamespace(runner="runner-b@host-a", host="host-a"), ttl=900)
        self.assertTrue(first.acquire(object(), now=1000))
        self.assertFalse(second.acquire(object(), now=1000))
        self.assertEqual(second.epoch, None)
        self.assertEqual(second.next_at, 1060)
        self.assertTrue(first.renew(object(), now=1100))
        self.assertEqual(first.epoch, 1)
        self.assertTrue(first.release(object()))
        self.assertTrue(second.acquire(object(), now=1200))

    def test_lost_epoch_is_not_silently_renewed(self):
        lease = train_runner.HostLease(self.cfg, ttl=900)
        self.assertTrue(lease.acquire(object(), now=1000))
        self.ops.locks.clear()
        self.assertFalse(lease.renew(object(), now=1100))
        self.assertIsNone(lease.epoch)
        self.assertEqual(lease.next_at, 1160)
        self.assertFalse(lease.release(object()))


class FakeLease:
    retry = 10.0

    def __init__(self, *, acquire=(True,), renew=(True,)):
        self.epoch = None
        self.next_at = 0.0
        self._acquire = list(acquire)
        self._renew = list(renew)
        self.calls = []

    def acquire(self, db, now):
        self.calls.append(("acquire", now))
        value = self._acquire.pop(0) if self._acquire else False
        if value:
            self.epoch, self.next_at = 1, now + 100
        return value

    def renew(self, db, now):
        self.calls.append(("renew", now))
        value = self._renew.pop(0) if self._renew else False
        if value:
            self.next_at = now + 100
        else:
            self.epoch = None
        return value

    def release(self, db):
        self.calls.append(("release", None))
        self.epoch = None
        return True

    def close(self, db, *, timeout=None):
        return self.release(db) if self.epoch is not None else False

    @contextlib.contextmanager
    def effect(self):
        yield


class ServiceLeaseTest(unittest.TestCase):
    def service(self, lease, clock):
        passage = types.SimpleNamespace(policy=types.SimpleNamespace(enabled=True))
        service = train_runner.Service(object(), [passage], stop=threading.Event(),
                                       report=mock.Mock(), clock=clock, lease=lease)
        return service

    def test_service_waits_for_the_lease_and_releases_it_on_stop(self):
        lease = FakeLease(acquire=(False, True))
        stops = []
        service = self.service(lease, lambda: 100)

        def pass_once(index, now):
            stops.append(index)
            service.stop.set()
            return None

        with mock.patch("ameesh.scoped_db.ScopedDb") as connect, \
             mock.patch.object(service.condition, "wait", return_value=False), \
             mock.patch.object(service, "pass_once", side_effect=pass_once) as passage:
            service.run()
        self.assertEqual(stops, [0])
        passage.assert_called_once()
        self.assertEqual([call[0] for call in lease.calls],
                         ["acquire", "acquire", "release"])
        self.assertGreaterEqual(connect.call_count, 3)

    def test_service_renews_before_a_passage_and_never_passes_when_lost(self):
        lease = FakeLease(acquire=(True, True), renew=(True,))
        service = self.service(lease, lambda: 100)
        lease.epoch, lease.next_at = 1, 50

        def pass_once(index, now):
            service.stop.set()
            return None

        with mock.patch("ameesh.scoped_db.ScopedDb"), \
             mock.patch.object(service.condition, "wait", return_value=False), \
             mock.patch.object(service, "pass_once", side_effect=pass_once) as passage:
            service.run()
        self.assertEqual([call[0] for call in lease.calls],
                         ["renew", "release"])
        passage.assert_called_once()

    def test_lost_lease_blocks_every_passage_until_a_new_acquisition(self):
        lease = FakeLease(acquire=(False, False, False), renew=(False,))
        service = self.service(lease, lambda: 100)
        lease.epoch, lease.next_at = 1, 50

        def wait(timeout=None):
            if len(lease.calls) > 5:
                service.stop.set()
            return False

        with mock.patch("ameesh.scoped_db.ScopedDb"), \
             mock.patch.object(service.condition, "wait", side_effect=wait), \
             mock.patch.object(service, "pass_once",
                               side_effect=AssertionError("passage while lease lost")) as passage:
            service.run()
        passage.assert_not_called()
        self.assertIn(("renew", 100), lease.calls)
        self.assertEqual(lease.calls[0], ("renew", 100))
        self.assertIn(("acquire", 100), lease.calls)
        self.assertNotIn(("release", None), lease.calls)
        self.assertIsNone(lease.epoch)


class LeaseFenceTest(unittest.TestCase):
    """#3134 : la fermeture attend l'effet en vol, puis refuse tous les suivants."""

    def setUp(self):
        self.ops = Ops()
        self.storage = mock.patch("ameesh.train_runner.storage.of",
                                  return_value=types.SimpleNamespace(work=self.ops))
        self.storage.start()
        self.addCleanup(self.storage.stop)
        self.cfg = types.SimpleNamespace(runner="runner-a@host-a", host="host-a")

    def test_close_waits_for_the_effect_in_flight_then_fences_and_frees(self):
        lease = train_runner.HostLease(self.cfg, ttl=900)
        self.assertTrue(lease.acquire(object(), now=1000))
        inside, leave = threading.Event(), threading.Event()

        def push():
            with lease.effect():
                inside.set()
                leave.wait(5)

        thread = threading.Thread(target=push)
        thread.start()
        self.assertTrue(inside.wait(5))
        # A push in flight keeps the lease: it is never released under a write.
        self.assertIsNone(lease.close(object(), timeout=0.05))
        self.assertIn("train-service", self.ops.locks)
        leave.set()
        thread.join(5)
        self.assertTrue(lease.close(object(), timeout=1))
        self.assertNotIn("train-service", self.ops.locks)
        with self.assertRaises(train.TrainError):
            with lease.effect():
                self.fail("effect after the close")
        self.assertFalse(lease.acquire(object(), now=1100))
        other = train_runner.HostLease(self.cfg, ttl=900)
        self.assertTrue(other.acquire(object(), now=1100))

    def test_fenced_runtime_refuses_every_write_after_the_close(self):
        lease = train_runner.HostLease(self.cfg, ttl=900)
        self.assertTrue(lease.acquire(object(), now=1000))
        calls = []

        def port(names):
            port = types.SimpleNamespace()
            for name in names:
                setattr(port, name, lambda *a, name=name, **k: calls.append(name) or name)
            return port

        runtime, ledger, repository = train_runner._FENCED
        fenced = port(runtime + ("load", "ci"))
        fenced.ledger = port(ledger + ("load",))
        fenced.repository = port(repository + ("target",))
        self.assertIs(train_runner._fenced(fenced, lease.effect), fenced)
        self.assertEqual(fenced.save(), "save")
        self.assertTrue(lease.close(object()))
        for owner, names in ((fenced, runtime), (fenced.ledger, ledger),
                             (fenced.repository, repository)):
            for name in names:
                with self.assertRaises(train.TrainError):
                    getattr(owner, name)(None)
        self.assertEqual(calls, ["save"])
        # Reads stay open: only the old holder's writes are fenced.
        self.assertEqual((fenced.load(), fenced.ci(), fenced.ledger.load(),
                          fenced.repository.target()), ("load", "ci", "load", "target"))


class HostLeaseSqlTest(PgTestCase):
    """La sémantique réelle de `merge_locks` : même runner, incarnations distinctes."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE merge_locks RESTART IDENTITY CASCADE")

    def test_two_incarnations_of_the_same_runner_cannot_both_hold(self):
        cfg = types.SimpleNamespace(runner="runner-a@host-a", host="host-a")
        first = train_runner.HostLease(cfg, ttl=900)
        second = train_runner.HostLease(cfg, ttl=900)
        self.assertNotEqual(first.owner, second.owner)
        self.assertTrue(first.acquire(self.db, now=1000))
        self.assertFalse(second.acquire(self.db, now=1000))
        self.assertEqual(first.epoch, 1)
        self.assertIsNone(second.epoch)
        # Codex B2 repro: the same owner would have obtained epoch 2 here.
        self.assertTrue(first.renew(self.db, now=1100))
        self.assertTrue(first.release(self.db))
        self.assertTrue(second.acquire(self.db, now=1200))

    def test_a_live_lease_blocks_another_incarnation(self):
        cfg = types.SimpleNamespace(runner="runner-a@host-a", host="host-a")
        first = train_runner.HostLease(cfg, ttl=900)
        second = train_runner.HostLease(cfg, ttl=900)
        self.assertTrue(first.acquire(self.db, now=1000))
        # The row is live even if the first process never renews again; the
        # second incarnation must wait for expiry instead of bumping the epoch.
        self.db.execute("UPDATE merge_locks SET expires_at = clock_timestamp()"
                        " + interval '10 minutes' WHERE target = %s", ("train-service",))
        self.assertFalse(second.acquire(self.db, now=1100))
        self.assertTrue(first.renew(self.db, now=1100))


class RunnerShutdownSqlTest(PgTestCase):
    """#3134 : l'arrêt de l'exécuteur rend le bail même si une passe dure."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE merge_locks RESTART IDENTITY CASCADE")

    def test_shutdown_during_a_passage_frees_the_row_and_fences_the_old_holder(self):
        lease = train_runner.HostLease(self.cfg, ttl=900)
        entered, leave, done = threading.Event(), threading.Event(), threading.Event()
        writes = []

        class Passage:
            policy = types.SimpleNamespace(enabled=True)

            def scheduled_step(self, db, now):
                entered.set()
                try:
                    leave.wait(10)  # a CI read or a push still running
                    with self.guard():
                        writes.append("save")
                    return {"result": "idle"}, now + 3600
                finally:
                    done.set()

        runner = Runner.__new__(Runner)
        runner.stop, runner.wake_all = threading.Event(), threading.Event()
        runner.listener = runner.listener_thread = None
        runner.lock, runner.workers, runner.turns = threading.Lock(), {}, 0
        runner.background = mock.Mock()
        report = mock.Mock()
        service = runner.train_service = train_runner.Service(
            self.cfg, [Passage()], stop=runner.stop, report=report, lease=lease)
        service.start()
        self.assertTrue(entered.wait(10))
        row = self.db.query("SELECT owner FROM merge_locks WHERE target = %s",
                            ("train-service",))
        self.assertEqual(row[0]["owner"], lease.owner)
        with mock.patch("ameesh.runner.log_async"):
            runner.shutdown()
        # The thread is still alive: `_sortie_sure` would exit immediately.
        self.assertIn(service.thread, runner.stopped_threads)
        self.assertTrue(service.thread.is_alive())
        row = self.db.query("SELECT owner FROM merge_locks WHERE target = %s",
                            ("train-service",))
        self.assertIsNone(row[0]["owner"])
        second = train_runner.HostLease(self.cfg, ttl=900)
        self.assertTrue(second.acquire(self.db, now=1000))
        leave.set()
        self.assertTrue(done.wait(10))
        service.thread.join(10)
        self.assertEqual(writes, [])
        self.assertTrue(any("stale train effect refused" in str(call.args[0])
                            for call in report.call_args_list))
        row = self.db.query("SELECT owner FROM merge_locks WHERE target = %s",
                            ("train-service",))
        self.assertEqual(row[0]["owner"], second.owner)


class RunnerWiringTest(unittest.TestCase):
    def test_shadow_passage_needs_no_reservation_and_receives_the_service_lease(self):
        runner = Runner.__new__(Runner)
        passage = types.SimpleNamespace(policy=train.Policy(enabled=True),
                                        shadow=True, reservation=None)
        runner.train_passages = [passage]
        runner.train_service = None
        runner.cfg = types.SimpleNamespace()
        runner.stop = threading.Event()
        runner.once = runner.dry_run = runner.mediated = False
        lease = object()
        runner.train_service_lease = lease
        with mock.patch("ameesh.train_runner.Service") as service:
            runner.start_train_service()
        service.assert_called_once()
        self.assertIs(service.call_args.kwargs["lease"], lease)


if __name__ == "__main__":
    unittest.main()
