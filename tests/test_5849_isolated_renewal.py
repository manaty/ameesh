# SPDX-License-Identifier: AGPL-3.0-only
"""Lease renewals bypass an occupied polling connection (#5849, #4440)."""
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import db as db_mod, migrations, registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase, scaled


class IsolatedRenewalTest(PgTestCase):
    def worker(self, name, runner=None):
        self.assertEqual(self.register(name, "claude").returncode, 0)
        runner = runner or Runner(self.cfg, self.db, once=True)
        runner.lease_ttl = 900
        lease = registry.claim(self.db, name, runner.runner_id, 300)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        worker.stop_group_now = mock.Mock()
        return worker

    def test_two_agents_renew_before_polling_connection_is_released(self):
        if self.db.name != "psycopg":
            self.skipTest("shared Python transaction lock belongs to psycopg")
        first = self.worker("choosing")
        workers = [first, self.worker("rotating", first.runner)]
        locked, release = threading.Event(), threading.Event()
        polling, polled = threading.Event(), threading.Event()
        failures = []

        def prepare():
            try:
                with self.db.transaction() as tx:
                    tx.query("SELECT 1")
                    locked.set()
                    if not release.wait(scaled(30)):
                        raise AssertionError("preparation was never released")
            except BaseException as exc:
                failures.append(exc)

        def poll():
            try:
                polling.set()
                self.db.query("SELECT 1")
                polled.set()
            except BaseException as exc:
                failures.append(exc)

        prepare_thread = threading.Thread(target=prepare, daemon=True)
        poll_thread = threading.Thread(target=poll, daemon=True)
        connections = []
        connect = db_mod.connect

        def opened(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.close = mock.Mock(wraps=connection.close)
            connections.append(connection)
            return connection

        prepare_thread.start()
        try:
            self.assertTrue(locked.wait(scaled(10)))
            poll_thread.start()
            self.assertTrue(polling.wait(scaled(10)))
            # Both the writer admission and the real registry.renew must
            # finish while this transaction still owns the polling lock.
            with mock.patch.object(db_mod, "connect", side_effect=opened):
                for worker in workers:
                    worker.call_budget = lambda: scaled(5)
                    before = worker.lease_deadline
                    started = time.monotonic()
                    self.assertTrue(worker.renew())
                    self.assertLess(time.monotonic() - started, scaled(5))
                    self.assertGreater(worker.lease_deadline, before)
                    self.assertFalse(worker.lease_lost.is_set())
                    worker.stop_group_now.assert_not_called()
            self.assertFalse(polled.is_set(), "polling unexpectedly escaped its lock")
            self.assertEqual(len(connections), 2)
            for connection in connections:
                self.assertIsNot(connection, self.db)
                connection.close.assert_called_once()
        finally:
            release.set()
            prepare_thread.join(scaled(10))
            if poll_thread.ident is not None:
                poll_thread.join(scaled(10))
            for worker in workers:
                for thread in worker.renewals:
                    thread.join(scaled(10))
        self.assertEqual(failures, [])
        self.assertTrue(polled.is_set())
        for worker in workers:
            row = registry.get(self.db, worker.name)
            self.assertEqual(row["lease_owner"], worker.runner.runner_id)
            self.assertEqual(row["lease_epoch"], worker.epoch)
            self.assertAlmostEqual(row["lease_expires_ts"], worker.lease_deadline)

    def test_isolated_connection_preserves_epoch_fencing(self):
        worker = self.worker("fenced")
        worker.epoch += 1
        self.assertFalse(worker.renew())
        self.assertTrue(worker.lease_lost.is_set())
        row = registry.get(self.db, worker.name)
        self.assertEqual(row["lease_epoch"], worker.epoch - 1)

    def test_schema_guard_still_refuses_before_renewing(self):
        worker = self.worker("schema")
        self.db.execute(
            "INSERT INTO schema_migrations (version, name, checksum) "
            "VALUES (9999, 'future', %s)", ("f" * 64,))
        self.db.execute(
            "INSERT INTO schema_non_additive_migrations (version, name, checksum) "
            "VALUES (9999, 'future', %s)", ("f" * 64,))
        try:
            answered, expires, error = worker._renew_call()
            self.assertTrue(answered)
            self.assertIsNone(expires)
            self.assertIsInstance(error, migrations.IncompatibleSchema)
        finally:
            self.db.execute("DELETE FROM schema_migrations WHERE version = 9999")

    def test_polling_passes_do_not_add_renewals(self):
        worker = self.worker("idle")
        passes = []

        def pick():
            passes.append(None)
            if len(passes) == 3:
                worker.stopping.set()
            return None

        with mock.patch.object(worker, "ensure_watchdog"), \
                mock.patch.object(worker, "ensure_peek"), \
                mock.patch.object(worker.runner, "gate_holds", return_value=False), \
                mock.patch.object(worker, "placed_elsewhere", return_value=""), \
                mock.patch.object(worker, "admission_withdrawn", return_value=""), \
                mock.patch.object(worker, "maybe_rotate"), \
                mock.patch.object(worker, "_election_handover", return_value=None), \
                mock.patch.object(worker, "_pick_for_turn", side_effect=pick), \
                mock.patch.object(worker, "_resident_between_turns"), \
                mock.patch.object(worker, "wait_timeout", return_value=0), \
                mock.patch.object(worker, "renew") as renew:
            worker._run_under_heartbeat()
        self.assertEqual(len(passes), 3)
        renew.assert_not_called()


class RenewalConnectionLifetimeTest(unittest.TestCase):
    def test_failed_and_detached_calls_close_their_own_connection(self):
        worker = types.SimpleNamespace(
            db=types.SimpleNamespace(cfg=types.SimpleNamespace(backend="postgres")),
            name="lease", epoch=1,
            runner=types.SimpleNamespace(runner_id="runner", lease_ttl=90),
            renewals=[], lease_held_until=0,
            _held_until=lambda: 90, call_budget=lambda: 0)
        entered, release = threading.Event(), threading.Event()
        connection = mock.Mock()

        def admitted(db):
            entered.set()
            release.wait(10)
            raise db_mod.Unavailable("offline")

        with mock.patch.object(db_mod, "connect", return_value=connection), \
                mock.patch.object(migrations, "require_compatible_writer", side_effect=admitted):
            try:
                self.assertEqual(AgentWorker._renew_call(worker), (False, None, None))
                self.assertTrue(entered.wait(10))
                connection.close.assert_not_called()
            finally:
                release.set()
                for thread in worker.renewals:
                    thread.join(10)
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
