# SPDX-License-Identifier: AGPL-3.0-only
"""Database-wide migration election, reconciliation and release guard."""
import concurrent.futures
import dataclasses
import contextlib
import io
from pathlib import Path
import tempfile
import threading
import time
import uuid
from unittest import mock

from ameesh import cli, db as db_mod, migrations
from .support import PgTestCase


class MigrationWorkerTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def migration(self, version, sql):
        Path(self.directory.name, f'{version:04d}_fixture.sql').write_text(sql)

    def fresh(self, timeout=5000):
        schema = 't_worker_' + uuid.uuid4().hex[:12]
        cfg = dataclasses.replace(self.cfg, schema=schema, statement_timeout_ms=timeout)
        self.addCleanup(self.db.execute, 'DROP SCHEMA IF EXISTS "' + schema + '" CASCADE')
        return cfg

    def test_second_worker_cannot_read_pending_before_first_commits(self):
        self.migration(1, 'CREATE TABLE only_once (id integer);')
        cfg = self.fresh()
        entered, release, second_read = threading.Event(), threading.Event(), threading.Event()
        connected, contender = threading.Event(), {}
        original = migrations.pending

        def pending(db, directory):
            if threading.current_thread().name == 'first-worker':
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test synchronization timeout')
            else:
                second_read.set()
            return original(db, directory)

        def run(name):
            threading.current_thread().name = name
            db = db_mod.connect(cfg)
            try:
                if name == 'second-worker':
                    original_transaction = db.transaction

                    @contextlib.contextmanager
                    def observed_transaction():
                        with original_transaction() as tx:
                            contender['pid'] = tx.query('SELECT pg_backend_pid() AS pid')[0]['pid']
                            connected.set()
                            yield tx

                    db.transaction = observed_transaction
                return migrations.migrate(db, self.directory.name)
            finally:
                db.close()

        with mock.patch.object(migrations, 'pending', pending):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                first = pool.submit(run, 'first-worker')
                try:
                    self.assertTrue(entered.wait(10))
                    second = pool.submit(run, 'second-worker')
                    self.assertTrue(connected.wait(10))
                    # Wait for the real PostgreSQL contender, not a timing-only check.
                    deadline = time.monotonic() + 10
                    waiting = False
                    while not second_read.is_set() and time.monotonic() < deadline:
                        waiting = self.db.query(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                            "AND NOT granted AND pid=%s) AS waiting", (contender['pid'],))[0]['waiting']
                        if waiting:
                            break
                        time.sleep(0.02)
                    self.assertFalse(second_read.is_set(), 'contender read inventory before election')
                    self.assertTrue(waiting, 'second worker never contended for database lock')
                finally:
                    release.set()
                self.assertEqual([m.version for m in first.result(10)], [1])
                self.assertEqual(second.result(10), [])

    def test_failure_rolls_back_inventory_and_ddl_and_releases_lock(self):
        self.migration(1, 'CREATE TABLE atomic_batch (id integer);')
        self.migration(2, 'SELECT 1/0;')
        cfg = self.fresh()
        db = db_mod.connect(cfg)
        try:
            messages = []
            with self.assertRaises(db_mod.DbError):
                migrations.migrate(db, self.directory.name, log=messages.append)
            self.assertEqual(messages, [], 'uncommitted migrations advertised as applied')
            self.assertFalse(self.db.query(
                'SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=%s) AS present',
                (cfg.schema,))[0]['present'])
            self.migration(2, 'CREATE TABLE after_retry (id integer);')
            self.assertEqual([m.version for m in migrations.migrate(db, self.directory.name)], [1, 2])
        finally:
            db.close()

    def test_additive_only_refuses_a_guard_on_an_existing_table(self):
        # #5142
        self.migration(1, 'CREATE TABLE untouched (id integer);')
        self.migration(600, 'CREATE RULE r AS ON INSERT TO untouched DO NOTHING;')
        db = db_mod.connect(self.fresh())
        try:
            with self.assertRaises(migrations.NonAdditiveMigration):
                migrations.migrate(db, self.directory.name, additive_only=True)
        finally:
            db.close()

    def test_additive_guard_runs_inside_elected_transaction_without_bootstrap(self):
        self.migration(1, 'CREATE TABLE untouched (id integer);')
        self.migration(2, 'DROP TABLE untouched;')
        cfg = self.fresh()
        db = db_mod.connect(cfg)
        try:
            with self.assertRaises(migrations.NonAdditiveMigration):
                migrations.migrate(db, self.directory.name, additive_only=True)
            self.assertFalse(self.db.query(
                'SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=%s) AS present',
                (cfg.schema,))[0]['present'])
        finally:
            db.close()

    def test_lost_writer_backend_rolls_back_and_reconnect_reconciles(self):
        self.migration(1, 'CREATE TABLE prior_committed (id integer);')
        cfg = self.fresh()
        db = db_mod.connect(cfg)
        try:
            migrations.migrate(db, self.directory.name)
            self.migration(2, 'CREATE TABLE unfinished (id integer); '
                           'SELECT pg_terminate_backend(pg_backend_pid());')
            messages = []
            with self.assertRaises(db_mod.DbError):
                migrations.migrate(db, self.directory.name, log=messages.append)
            self.assertEqual(messages, [])
        finally:
            db.close()
        # Both drivers open a new connection; no production backend is killed.
        db = db_mod.connect(cfg)
        try:
            self.assertEqual(set(migrations.applied(db)), {1})
            self.assertFalse(db.query("SELECT to_regclass('unfinished') IS NOT NULL AS present")[0]['present'])
            self.migration(2, 'CREATE TABLE unfinished (id integer);')
            self.assertEqual([m.version for m in migrations.migrate(db, self.directory.name)], [2])
        finally:
            db.close()

    def test_cli_rechecks_additive_guard_after_outdated_preflight(self):
        self.migration(1, 'CREATE TABLE untouched (id integer);')
        self.migration(2, 'DROP TABLE untouched;')
        cfg = self.fresh()
        discover = migrations.discover
        with mock.patch.object(migrations, 'check', return_value=([], [], [])), \
                mock.patch.object(migrations, 'discover', side_effect=lambda *_: discover(self.directory.name)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_migrate(cfg, additive_only=True), 3)
        self.assertFalse(self.db.query(
            'SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=%s) AS present',
            (cfg.schema,))[0]['present'])

    def test_database_election_shared_across_schema_aliases_and_bounded(self):
        self.migration(1, 'CREATE TABLE bounded (id integer);')
        cfg = self.fresh(timeout=150)
        db = db_mod.connect(cfg)
        try:
            # Existing lock key is shared with earlier code and scoped by the
            # server to its actual database, independently of schema/DSN labels.
            with self.db.transaction() as owner:
                owner.query("SELECT pg_advisory_xact_lock(hashtext(%s))", (migrations._LOCK,))
                started = time.monotonic()
                with self.assertRaises(db_mod.DbError):
                    migrations.migrate(db, self.directory.name)
                self.assertLess(time.monotonic() - started, 3)
                self.assertFalse(owner.query(
                    'SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=%s) AS present',
                    (cfg.schema,))[0]['present'])
            self.assertEqual([m.version for m in migrations.migrate(db, self.directory.name)], [1])
        finally:
            db.close()
