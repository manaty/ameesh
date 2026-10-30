# SPDX-License-Identifier: AGPL-3.0-only
"""#4975: process admission, fresh ticks and migration/executor exclusion."""
from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from ameesh import cli, config, db as db_mod, migrations, runner
from .support import child_env, REQUIRE_DB

HAVE_PSYCOPG = importlib.util.find_spec('psycopg') is not None
DRIVERS = ('psycopg', 'psql') if HAVE_PSYCOPG else ('psql',)


class SchemaGuardTest(unittest.TestCase):
    def setUp(self):
        self.schema = 't_schema_guard_' + uuid.uuid4().hex[:12]
        self.cfg = config.load(env=child_env(AMEESH_SCHEMA=self.schema))
        try:
            self.admin = db_mod.connect(self.cfg, writer=False)
        except db_mod.Unavailable as exc:
            if not REQUIRE_DB:
                self.skipTest(str(exc))
            raise
        self.addCleanup(self.cleanup)
        self.admin.script('CREATE SCHEMA %s; SET search_path TO %s;'
                          'CREATE TABLE schema_migrations (version integer PRIMARY KEY, name text, checksum text);'
                          'CREATE TABLE agent_registry (name text, lease_owner text, lease_expires_at timestamptz);'
                          'CREATE TABLE guard_writes (n integer); INSERT INTO guard_writes VALUES (0);'
                          'CREATE TABLE ameesh_events (type text, object text, data jsonb);'
                          % (self.schema, self.schema))
        self.admin.script(Path(migrations.MIGRATIONS_DIR, '0524_schema_write_guard.sql').read_text())

    def cleanup(self):
        self.admin.execute('DROP SCHEMA IF EXISTS %s CASCADE' % self.schema)
        self.admin.close()

    def mark_unknown(self, *, additive=False):
        self.admin.execute('INSERT INTO schema_migrations VALUES (9999, %s, %s)', ('future', 'f' * 64))
        if not additive:
            self.admin.execute('INSERT INTO schema_non_additive_migrations VALUES (9999, %s, %s)',
                               ('future', 'f' * 64))

    def test_unknown_non_additive_refuses_process_start_on_both_drivers(self):
        self.mark_unknown()
        for driver in DRIVERS:
            with self.subTest(driver=driver), self.assertRaisesRegex(migrations.IncompatibleSchema, '9999_future'):
                db_mod.connect(self.cfg, driver=driver)
        self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 0}])

    def test_live_executor_and_timer_refuse_next_tick_before_any_side_effect(self):
        for driver in DRIVERS:
            with self.subTest(driver=driver):
                writer = db_mod.connect(self.cfg, driver=driver)
                try:
                    # An existing process must reread the marker, not a cached
                    # startup verdict. Real timer/turn boundaries refuse before
                    # touching their other state or invoking writing functions.
                    self.mark_unknown()
                    process = SimpleNamespace(db=writer)
                    for method in (runner.Runner.expire_operator_pauses_once,
                                   runner.Runner.sweep, runner.AgentWorker.run_turn):
                        with self.assertRaisesRegex(migrations.IncompatibleSchema, '9999_future'):
                            method(process, {}) if method is runner.AgentWorker.run_turn else method(process)
                finally:
                    writer.close()
                    self.admin.execute('DELETE FROM schema_non_additive_migrations')
                    self.admin.execute('DELETE FROM schema_migrations')
        self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 0}])

    def test_additive_ahead_remains_writable_at_start_and_tick(self):
        self.mark_unknown(additive=True)
        for driver in DRIVERS:
            with self.subTest(driver=driver):
                writer = db_mod.connect(self.cfg, driver=driver)
                try:
                    migrations.require_compatible_writer(writer)
                    writer.execute('UPDATE guard_writes SET n = 3')
                finally:
                    writer.close()
        self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 3}])

    def test_one_select_per_tick_and_no_per_statement_or_transaction_guard(self):
        writer = db_mod.connect(self.cfg)
        try:
            before = db_mod.TRACE.round_trips
            migrations.require_compatible_writer(writer)
            self.assertEqual(db_mod.TRACE.round_trips - before, 1)
            before = db_mod.TRACE.round_trips
            writer.execute('UPDATE guard_writes SET n = 4')
            self.assertEqual(db_mod.TRACE.round_trips - before, 1)
            with writer.transaction() as tx:
                tx.execute('SAVEPOINT recovery')
                tx.execute('ROLLBACK TO SAVEPOINT recovery')
                tx.execute('UPDATE guard_writes SET n = 5')
        finally:
            writer.close()

    def test_nested_passes_share_one_check_but_next_tick_is_fresh(self):
        class Process:
            def __init__(self, db):
                self.db = db
            @migrations.writer_tick
            def tick(self):
                self.child()
            @migrations.writer_tick
            def child(self):
                pass
        writer = db_mod.connect(self.cfg)
        try:
            process = Process(writer)
            before = db_mod.TRACE.round_trips
            process.tick()
            self.assertEqual(db_mod.TRACE.round_trips - before, 1)
            self.mark_unknown()
            with self.assertRaises(migrations.IncompatibleSchema):
                process.tick()
        finally:
            writer.close()

    def test_restricted_process_requires_marker_read_only(self):
        role = 't_guard_writer_' + uuid.uuid4().hex[:12]
        q = db_mod.quote_ident
        self.admin.execute('CREATE ROLE %s NOLOGIN' % q(role))
        self.admin.execute('GRANT USAGE ON SCHEMA %s TO %s' % (q(self.schema), q(role)))
        self.admin.execute('GRANT INSERT ON agent_registry TO %s' % q(role))
        try:
            for driver in DRIVERS:
                writer = db_mod.connect(self.cfg, driver=driver, writer=False)
                try:
                    with self.assertRaisesRegex(migrations.IncompatibleSchema, 'marker unreadable'):
                        with writer.transaction() as tx:
                            tx.execute('SET LOCAL ROLE %s' % q(role))
                            migrations.require_compatible_writer(tx)
                    self.admin.execute('GRANT SELECT ON schema_non_additive_migrations TO %s' % q(role))
                    with writer.transaction() as tx:
                        tx.execute('SET LOCAL ROLE %s' % q(role))
                        migrations.require_compatible_writer(tx)
                        tx.execute("INSERT INTO agent_registry(name) VALUES ('accepted')")
                    self.assertFalse(self.admin.query(
                        "SELECT has_table_privilege(%s, 'schema_non_additive_migrations', 'INSERT,UPDATE,DELETE') AS p",
                        (role,))[0]['p'])
                    self.admin.execute('REVOKE SELECT ON schema_non_additive_migrations FROM %s' % q(role))
                finally:
                    writer.close()
        finally:
            self.admin.execute('DROP OWNED BY %s' % q(role))
            self.admin.execute('DROP ROLE %s' % q(role))

    def test_writing_select_is_stopped_at_process_tick(self):
        self.admin.execute("CREATE FUNCTION guard_mutate() RETURNS integer LANGUAGE plpgsql AS "
                           "$$ BEGIN UPDATE guard_writes SET n = n + 1; RETURN 1; END $$")
        class Process:
            def __init__(self, db):
                self.db = db
            @migrations.writer_tick
            def tick(self):
                return self.db.query('SELECT guard_mutate() AS n')
        for driver in DRIVERS:
            writer = db_mod.connect(self.cfg, driver=driver)
            try:
                process = Process(writer)
                self.assertEqual(process.tick(), [{'n': 1}])
                self.mark_unknown()
                with self.assertRaises(migrations.IncompatibleSchema):
                    process.tick()
                self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 1}])
            finally:
                writer.close()
                self.admin.execute('DELETE FROM schema_non_additive_migrations')
                self.admin.execute('DELETE FROM schema_migrations')
                self.admin.execute('UPDATE guard_writes SET n = 0')

    def test_reused_mediated_connection_refuses_next_request(self):
        from ameesh.mediated_executor.dispatcher import ConnectionPool
        pool = ConnectionPool(lambda: db_mod.connect(self.cfg), size=1,
                              before_use=migrations.require_compatible_writer)
        try:
            with pool.connection() as first:
                first.execute('UPDATE guard_writes SET n = 1')
            self.mark_unknown()
            with self.assertRaises(migrations.IncompatibleSchema):
                with pool.connection():
                    self.fail('request admitted on incompatible reused connection')
            self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 1}])
        finally:
            pool.close()

    def test_missing_marker_after_installation_is_not_bootstrap(self):
        self.admin.execute('INSERT INTO schema_migrations VALUES (524, %s, %s)', ('schema_write_guard', 'f' * 64))
        self.admin.execute('DROP TABLE schema_non_additive_migrations')
        with self.assertRaisesRegex(migrations.IncompatibleSchema, 'marker unreadable'):
            db_mod.connect(self.cfg)

    def test_known_version_with_different_content_refuses_process(self):
        known = migrations.discover()[-1]
        self.admin.execute('INSERT INTO schema_migrations VALUES (%s,%s,%s)',
                           (known.version, known.name, '0' * 64))
        self.admin.execute('INSERT INTO schema_non_additive_migrations VALUES (%s,%s,%s)',
                           (known.version, known.name, '0' * 64))
        with self.assertRaises(migrations.IncompatibleSchema):
            db_mod.connect(self.cfg)

    def test_probe_and_doctor_name_incompatible_migration(self):
        self.mark_unknown()
        for diagnostic in (cli.cmd_probe, cli.cmd_doctor):
            output = io.StringIO()
            with mock.patch.object(cli, '_profile_audit', return_value=None), contextlib.redirect_stdout(output):
                result = (diagnostic(self.cfg, notify_test=False) if diagnostic is cli.cmd_doctor
                          else diagnostic(self.cfg))
            self.assertEqual(result, 1)
            self.assertIn('9999_future', output.getvalue())

    def test_marker_additive_and_worker_records_non_additive_atomically(self):
        marker = next(m for m in migrations.discover() if m.version == 524)
        self.assertEqual(migrations.non_additive(marker), [])
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '0001_break.sql').write_text('-- ameesh: non-additive\nUPDATE guard_writes SET n = 7;')
            migrations.migrate(self.admin, directory)
            self.assertEqual(self.admin.query('SELECT version FROM schema_non_additive_migrations'), [{'version': 1}])
            migrations.require_compatible_writer(self.admin, directory)
            with self.assertRaises(migrations.IncompatibleSchema):
                db_mod.connect(self.cfg)

    def test_failed_migration_rolls_back_marker_and_data(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '0001_break.sql').write_text('-- ameesh: non-additive\nUPDATE guard_writes SET n = 7;')
            Path(directory, '0002_fail.sql').write_text('SELECT missing_column;')
            with self.assertRaises(db_mod.DbError):
                migrations.migrate(self.admin, directory)
        self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 0}])
        self.assertEqual(self.admin.query('SELECT * FROM schema_non_additive_migrations'), [])

    def test_live_lease_refuses_non_additive_but_additive_is_allowed(self):
        self.admin.execute("INSERT INTO agent_registry VALUES ('active', 'executor', clock_timestamp() + interval '1 hour')")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '0001_add.sql').write_text('CREATE TABLE additive_fixture(n integer);')
            migrations.migrate(self.admin, directory)
            Path(directory, '0002_break.sql').write_text('-- ameesh: non-additive\nUPDATE guard_writes SET n = 7;')
            with self.assertRaisesRegex(migrations.LiveExecutors, 'active'):
                migrations.migrate(self.admin, directory)
            self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 0}])
            self.assertEqual(self.admin.query('SELECT * FROM schema_non_additive_migrations'), [])

    def test_operator_override_requires_reason_and_records_same_transaction(self):
        self.admin.execute("INSERT INTO agent_registry VALUES ('active', 'executor', clock_timestamp() + interval '1 hour')")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '0001_break.sql').write_text('-- ameesh: non-additive\nUPDATE guard_writes SET n = 7;')
            with self.assertRaises((ValueError, migrations.LiveExecutors)):
                migrations.migrate(self.admin, directory, allow_live_executors=' ')
            migrations.migrate(self.admin, directory, allow_live_executors='incident recovery fixture')
            event = self.admin.query("SELECT data FROM ameesh_events WHERE type='schema.migration_override'")[0]['data']
            self.assertEqual(event['reason'], 'incident recovery fixture')
            self.assertEqual(event['live_leases'], [{'name': 'active', 'lease_owner': 'executor'}])
            self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 7}])

    def test_expired_lease_does_not_block_non_additive(self):
        self.admin.execute("INSERT INTO agent_registry VALUES ('expired', 'executor', clock_timestamp() - interval '1 second')")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '0001_break.sql').write_text('-- ameesh: non-additive\nUPDATE guard_writes SET n = 7;')
            migrations.migrate(self.admin, directory)
        self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 7}])

    @unittest.skipUnless(HAVE_PSYCOPG, 'independent concurrent lease admission')
    def test_lease_committed_while_migration_waits_is_seen_after_lock(self):
        admission = db_mod.connect(self.cfg, driver='psycopg', writer=False)
        migrator = db_mod.connect(self.cfg, driver='psycopg', writer=False)
        self.addCleanup(admission.close)
        self.addCleanup(migrator.close)
        reached = threading.Event()
        errors = []
        original = migrations._exclude_live_executors
        def check(tx, todo, reason):
            reached.set()
            return original(tx, todo, reason)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '0001_break.sql').write_text('-- ameesh: non-additive\nUPDATE guard_writes SET n = 7;')
            def run():
                try:
                    migrations.migrate(migrator, directory)
                except Exception as exc:
                    errors.append(exc)
            with mock.patch.object(migrations, '_exclude_live_executors', side_effect=check):
                with admission.transaction() as tx:
                    tx.execute("INSERT INTO agent_registry VALUES ('racing', 'executor', clock_timestamp() + interval '1 hour')")
                    thread = threading.Thread(target=run)
                    thread.start()
                    self.assertTrue(reached.wait(3))
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], migrations.LiveExecutors)
            self.assertIn('racing', str(errors[0]))
            self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 0}])

    @unittest.skipUnless(HAVE_PSYCOPG, 'independent observer uses psycopg lock_timeout')
    def test_migration_excludes_new_admission_until_commit(self):
        observer = db_mod.connect(self.cfg, driver='psycopg', writer=False)
        self.addCleanup(observer.close)
        original = migrations._exclude_live_executors
        def check(tx, todo, reason):
            original(tx, todo, reason)
            # Already past the live-lease check. A new agent/claim must be
            # excluded through the migration COMMIT, not only existing rows.
            with self.assertRaises(db_mod.DbError):
                with observer.transaction() as other:
                    other.execute("SET LOCAL lock_timeout = '100ms'")
                    other.execute("INSERT INTO agent_registry VALUES ('new', 'new-executor', clock_timestamp() + interval '1 hour')")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '0001_break.sql').write_text('-- ameesh: non-additive\nUPDATE guard_writes SET n = 7;')
            with mock.patch.object(migrations, '_exclude_live_executors', side_effect=check):
                migrations.migrate(self.admin, directory)
        observer.execute("INSERT INTO agent_registry VALUES ('new', 'new-executor', clock_timestamp() + interval '1 hour')")
