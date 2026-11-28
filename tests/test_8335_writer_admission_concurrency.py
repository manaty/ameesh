# SPDX-License-Identifier: AGPL-3.0-only
"""#8335 : l'admission des écrivains ne sérialise plus les workers d'un processus."""
from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import config, db as db_mod, migrations

WORKERS = 64
READ_S = 0.05


class WriterAdmissionConcurrencyTest(unittest.TestCase):
    def setUp(self):
        for name, value in (('_WRITER_ADMISSIONS', {}), ('_WRITER_ADMISSION_FLIGHTS', {})):
            patch = mock.patch.object(migrations, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.reads = 0
        self.inflight = 0
        self.peak = 0
        self.result = []
        self.counter = threading.Lock()
        check = mock.patch.object(migrations, 'incompatible_migrations', side_effect=self.read)
        check.start()
        self.addCleanup(check.stop)
        self.cfg = config.Config(dsn='postgresql://user@db/database', schema='public')

    def read(self, *args):
        with self.counter:
            self.reads += 1
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        time.sleep(READ_S)
        with self.counter:
            self.inflight -= 1
        return list(self.result)

    def run_workers(self, fn):
        """N fils démarrés ensemble ; rend les erreurs par fil."""
        barrier = threading.Barrier(WORKERS)
        errors = [None] * WORKERS
        def work(i):
            barrier.wait()
            try:
                fn(SimpleNamespace(cfg=self.cfg))
            except Exception as exc:  # noqa: BLE001 - rendu au test
                errors[i] = exc
        threads = [threading.Thread(target=work, args=(i,)) for i in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads), 'workers did not finish')
        return errors

    def test_concurrent_admissions_share_one_read(self):
        errors = self.run_workers(migrations.admit_writer_connection)
        self.assertEqual(errors, [None] * WORKERS)
        self.assertEqual(self.reads, 1)

    def test_explicit_checks_run_in_parallel_and_stay_fresh(self):
        errors = self.run_workers(migrations.require_compatible_writer)
        self.assertEqual(errors, [None] * WORKERS)
        self.assertEqual(self.reads, WORKERS)  # jamais sautés
        self.assertGreater(self.peak, 1)  # avant : verrou global, pic à 1

    def test_incompatible_schema_refuses_every_waiting_connection(self):
        self.result = [dict(version=9999, name='future', checksum='unknown')]
        errors = self.run_workers(migrations.admit_writer_connection)
        self.assertTrue(all(isinstance(e, migrations.IncompatibleSchema) for e in errors), errors)
        self.assertIn('9999_future', str(errors[0]))
        self.assertEqual(migrations._WRITER_ADMISSIONS, {})
        self.assertEqual(migrations._WRITER_ADMISSION_FLIGHTS, {})

    def test_success_read_before_a_concurrent_refusal_is_not_cached(self):
        db = SimpleNamespace(cfg=self.cfg)
        reading, refused = threading.Event(), threading.Event()
        def ok_then_wait(*args):
            reading.set()
            refused.wait(5)
            return []
        def refuse(*args):
            return [dict(version=9999, name='future', checksum='unknown')]
        with mock.patch.object(migrations, 'incompatible_migrations', side_effect=ok_then_wait):
            slow = threading.Thread(target=migrations.require_compatible_writer, args=(db,))
            slow.start()
            reading.wait(5)
        with mock.patch.object(migrations, 'incompatible_migrations', side_effect=refuse):
            with self.assertRaises(migrations.IncompatibleSchema):
                migrations.require_compatible_writer(db)
        refused.set()
        slow.join(5)
        self.assertEqual(migrations._WRITER_ADMISSIONS, {})

    def test_waiter_checks_itself_when_the_shared_read_fails(self):
        db = SimpleNamespace(cfg=self.cfg)
        calls = []
        def first_fails(*args):
            calls.append(threading.get_ident())
            if len(calls) == 1:
                time.sleep(READ_S)
                raise db_mod.Unavailable('leader connection lost')
            return []
        with mock.patch.object(migrations, 'incompatible_migrations', side_effect=first_fails):
            leader = threading.Thread(target=lambda: self.assertRaises(
                db_mod.Unavailable, migrations.admit_writer_connection, db))
            leader.start()
            while not migrations._WRITER_ADMISSION_FLIGHTS:
                time.sleep(0.001)
            migrations.admit_writer_connection(db)
            leader.join(5)
        self.assertEqual(len(calls), 2)

    def test_wait_is_bounded_then_the_connection_checks_itself(self):
        db = SimpleNamespace(cfg=self.cfg)
        key = migrations._writer_admission_key(db, migrations.MIGRATIONS_DIR)
        migrations._WRITER_ADMISSION_FLIGHTS[key] = (-1, threading.Event())  # vol bloqué
        with mock.patch.object(migrations, 'WRITER_ADMISSION_WAIT_S', 0.01):
            migrations.admit_writer_connection(db)
        self.assertEqual(self.reads, 1)

    def test_nested_admission_on_the_same_thread_does_not_wait_for_itself(self):
        # ScopedDb ouvre sa connexion pendant la lecture : l'admission
        # imbriquée du même fil ne doit pas attendre son propre vol (30 s).
        db = SimpleNamespace(cfg=self.cfg)
        calls = []

        def nested(*args):
            calls.append(threading.get_ident())
            if len(calls) == 1:
                migrations.admit_writer_connection(db)
            return []

        with mock.patch.object(migrations, 'incompatible_migrations', side_effect=nested):
            thread = threading.Thread(target=migrations.admit_writer_connection, args=(db,))
            started = time.monotonic()
            thread.start()
            thread.join(5)
            elapsed = time.monotonic() - started
        self.assertFalse(thread.is_alive(), 'nested admission waited on its own flight')
        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(migrations._WRITER_ADMISSION_FLIGHTS, {})


if __name__ == '__main__':
    unittest.main()
