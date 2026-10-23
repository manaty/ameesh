# SPDX-License-Identifier: AGPL-3.0-only
"""#2446 : un seul hôte exécute le service de train, sous bail exclusif."""
from __future__ import annotations

import contextlib
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import (train, train_git, train_migrations, train_reservation, train_runner,
                    train_runtime, train_store)
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

    def test_a_close_during_a_promotion_lets_its_nested_writes_finish(self):
        # Relecture mty-amh-impl-01 de 2d9f6837fc10 : promote -> ledger.acquire
        # et migrations.landed, tous deux fencés, après la fermeture.
        lease = train_runner.HostLease(self.cfg, ttl=900)
        self.assertTrue(lease.acquire(object(), now=1000))
        inside, leave = threading.Event(), threading.Event()
        calls, errors = [], []
        runtime = types.SimpleNamespace(
            ledger=types.SimpleNamespace(acquire=lambda epoch: calls.append("acquire") or epoch),
            repository=types.SimpleNamespace(promote=lambda: calls.append("push")),
            migrations=types.SimpleNamespace(landed=lambda: calls.append("landed")))

        def promote(epoch):
            inside.set()
            leave.wait(5)
            if runtime.ledger.acquire(epoch) != epoch:
                raise train.TrainError("target reservation lost before promotion")
            runtime.repository.promote()
            runtime.migrations.landed()

        runtime.promote = promote
        train_runner._fenced(runtime, lease.effect)

        def run():
            try:
                runtime.promote(1)
            except Exception as exc:  # pragma: no cover - the failure under test
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(inside.wait(5))
        self.assertIsNone(lease.close(object(), timeout=0))
        leave.set()
        self.assertTrue(lease.close(object(), timeout=5))
        thread.join(5)
        self.assertEqual((errors, calls), ([], ["acquire", "push", "landed"]))
        self.assertNotIn("train-service", self.ops.locks)
        # A new top-level effect, nested writes included, stays refused.
        for write in (lambda: runtime.promote(1), lambda: runtime.ledger.acquire(1),
                      runtime.migrations.landed):
            with self.assertRaises(train.TrainError):
                write()
        self.assertEqual(calls, ["acquire", "push", "landed"])

    #: The writes of a passage, named here rather than read from `_FENCED`.
    WRITES = {
        "runtime": (train_runtime.Runtime, ("save", "release", "enroll", "quarantine",
                                            "start_ci", "reconcile", "promote")),
        "ledger": (train_reservation.ReservedLedger, ("acquire", "before_publish")),
        "repository": (train_git.Repository, ("publish",)),
        "migrations": (train_migrations.Allocator, ("reserve", "landed")),
    }

    def test_every_write_of_the_real_ports_is_fenced(self):
        fenced = dict(zip(("runtime", "ledger", "repository", "migrations"),
                          train_runner._FENCED))
        for port, (cls, names) in self.WRITES.items():
            for name in names:
                self.assertTrue(callable(getattr(cls, name, None)), (cls, name))
            self.assertEqual(set(fenced[port]), set(names), port)
        self.assertTrue(callable(train_store.Ledger.acquire))

    def test_fenced_runtime_refuses_every_write_after_the_close(self):
        lease = train_runner.HostLease(self.cfg, ttl=900)
        self.assertTrue(lease.acquire(object(), now=1000))
        calls = []

        def port(names):
            port = types.SimpleNamespace()
            for name in names:
                setattr(port, name, lambda *a, name=name, **k: calls.append(name) or name)
            return port

        writes = {key: names for key, (_cls, names) in self.WRITES.items()}
        fenced = port(writes["runtime"] + ("load", "ci", "compose"))
        fenced.ledger = port(writes["ledger"] + ("load",))
        fenced.repository = port(writes["repository"] + ("target",))
        fenced.migrations = port(writes["migrations"])
        self.assertIs(train_runner._fenced(fenced, lease.effect), fenced)
        self.assertEqual(fenced.save(), "save")
        self.assertTrue(lease.close(object()))
        for key, owner in (("runtime", fenced), ("ledger", fenced.ledger),
                           ("repository", fenced.repository),
                           ("migrations", fenced.migrations)):
            for name in writes[key]:
                with self.assertRaises(train.TrainError):
                    getattr(owner, name)(None)
        self.assertEqual(calls, ["save"])
        # Reads and the local replay stay open: only the writes are fenced.
        self.assertEqual((fenced.load(), fenced.ci(), fenced.ledger.load(),
                          fenced.repository.target()), ("load", "ci", "load", "target"))

    def test_a_composition_resumed_after_the_close_cannot_reserve_a_migration(self):
        lease = train_runner.HostLease(self.cfg, ttl=900)
        self.assertTrue(lease.acquire(object(), now=1000))
        allocator = train_migrations.Allocator(object())
        runtime = types.SimpleNamespace(ledger=None, repository=None, migrations=allocator)
        train_runner._fenced(runtime, lease.effect)
        self.assertTrue(lease.close(object()))
        with mock.patch("ameesh.work.reserve_migration") as reserve:
            with self.assertRaises(train.TrainError):
                allocator.reserve({"work_item_id": 7}, "/clone", 1, 2, None)
        reserve.assert_not_called()


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

    def runner(self, passage, lease, report):
        runner = Runner.__new__(Runner)
        runner.stop, runner.wake_all = threading.Event(), threading.Event()
        runner.listener = runner.listener_thread = None
        runner.lock, runner.workers, runner.turns = threading.Lock(), {}, 0
        runner.background = mock.Mock()
        runner.train_service = train_runner.Service(
            self.cfg, [passage], stop=runner.stop, report=report, lease=lease)
        return runner

    def owner(self):
        row = self.db.query("SELECT owner FROM merge_locks WHERE target = %s",
                            ("train-service",))
        return row[0]["owner"]

    def test_shutdown_waits_for_an_effect_longer_than_seven_seconds(self):
        lease = train_runner.HostLease(self.cfg, ttl=900)
        entered, done = threading.Event(), threading.Event()
        writes = []

        class Passage:
            policy = types.SimpleNamespace(enabled=True)

            def scheduled_step(self, db, now):
                try:
                    with self.guard():  # a push or a promotion in flight
                        entered.set()
                        time.sleep(8)
                        writes.append("push")
                    with self.guard():
                        writes.append("after the stop")
                    return {"result": "idle"}, now + 3600
                finally:
                    done.set()

        runner = self.runner(Passage(), lease, mock.Mock())
        runner.train_service.start()
        self.assertTrue(entered.wait(10))
        started = time.monotonic()
        with mock.patch("ameesh.runner.log_async"):
            runner.shutdown()
        self.assertGreaterEqual(time.monotonic() - started, 6.0)
        self.assertEqual(writes, ["push"])
        self.assertIsNone(self.owner())
        second = train_runner.HostLease(self.cfg, ttl=900)
        self.assertTrue(second.acquire(self.db, now=1000))
        self.assertTrue(done.wait(10))
        runner.train_service.thread.join(10)
        self.assertEqual(writes, ["push"])
        self.assertEqual(self.owner(), second.owner)

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

        report = mock.Mock()
        runner = self.runner(Passage(), lease, report)
        service = runner.train_service
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
