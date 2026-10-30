# SPDX-License-Identifier: AGPL-3.0-only
"""#4975: persistent writers refuse unknown non-additive migrations."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import importlib.util
from pathlib import Path
import tempfile
import unittest
import uuid

from ameesh import cli, db as db_mod, migrations
from .support import child_env, REQUIRE_DB
from ameesh import config


HAVE_PSYCOPG = importlib.util.find_spec('psycopg') is not None
DRIVERS = ('psycopg', 'psql') if HAVE_PSYCOPG else ('psql',)


class SchemaGuardTest(unittest.TestCase):
    def setUp(self):
        self.schema = 't_schema_guard_' + uuid.uuid4().hex[:12]
        self.cfg = config.load(env=child_env(AMEESH_SCHEMA=self.schema))
        try:
            self.admin = db_mod.connect(self.cfg)
        except db_mod.Unavailable as exc:
            if not REQUIRE_DB:
                self.skipTest(str(exc))
            raise
        self.addCleanup(self.cleanup)
        with db_mod.schema_guard_bypass(self.admin):
            self.admin.script('CREATE SCHEMA %s; SET search_path TO %s;'
                              'CREATE TABLE schema_migrations (version integer PRIMARY KEY, '
                              'name text, checksum text);'
                              'CREATE TABLE agent_registry (name text);'
                              'CREATE TABLE guard_writes (n integer);'
                              'INSERT INTO guard_writes VALUES (0);'
                              % (self.schema, self.schema))
            self.admin.script(Path(migrations.MIGRATIONS_DIR,
                                   '0524_schema_write_guard.sql').read_text())

    def cleanup(self):
        with db_mod.schema_guard_bypass(self.admin):
            self.admin.execute('DROP SCHEMA IF EXISTS %s CASCADE' % self.schema)
        self.admin.close()

    def mark_unknown(self, *, additive=False):
        with db_mod.schema_guard_bypass(self.admin):
            self.admin.execute('INSERT INTO schema_migrations VALUES (9999, %s, %s)',
                               ('future', 'f' * 64))
            if not additive:
                self.admin.execute('INSERT INTO schema_non_additive_migrations '
                                   'VALUES (9999, %s, %s)', ('future', 'f' * 64))

    def test_persistent_timer_and_executor_connections_refuse_all_write_paths(self):
        for driver in DRIVERS:
            with self.subTest(driver=driver):
                writer = db_mod.connect(self.cfg, driver=driver)
                self.addCleanup(writer.close)
                writer.execute('UPDATE guard_writes SET n = 1')
                self.mark_unknown()
                for mutation in (
                    lambda: writer.execute('UPDATE guard_writes SET n = 2'),
                    lambda: writer.query('UPDATE guard_writes SET n = 2 RETURNING n'),
                    lambda: writer.query('WITH changed AS (UPDATE guard_writes SET n = 2 '
                                         'RETURNING n) SELECT * FROM changed'),
                    lambda: writer.script('UPDATE guard_writes SET n = 2;'),
                    lambda: writer.query_batch([('UPDATE guard_writes SET n = 2 RETURNING n', ())]),
                    lambda: writer.query_batch([('UPDATE guard_writes SET n = 2 RETURNING n', ()),
                                                ('UPDATE guard_writes SET n = 3 RETURNING n', ())]),
                ):
                    with self.assertRaisesRegex(migrations.IncompatibleSchema, '9999_future'):
                        mutation()
                with self.assertRaises(migrations.IncompatibleSchema):
                    with writer.transaction() as tx:
                        tx.execute('UPDATE guard_writes SET n = 2')
                self.assertEqual(writer.query('SELECT n FROM guard_writes'), [{'n': 1}])
                # Reset through the administrative migration-only exemption.
                with db_mod.schema_guard_bypass(self.admin):
                    self.admin.execute('DELETE FROM schema_non_additive_migrations')
                    self.admin.execute('DELETE FROM schema_migrations')

    @unittest.skipUnless(HAVE_PSYCOPG, 'psycopg-specific transaction/round-trip contract')
    def test_writer_keeps_migration_excluded_until_commit(self):
        writer = db_mod.connect(self.cfg, driver='psycopg')
        self.addCleanup(writer.close)
        with writer.transaction() as tx:
            tx.execute('UPDATE guard_writes SET n = 4')
            with db_mod.schema_guard_bypass(self.admin), self.admin.transaction() as observer:
                locked = observer.query('SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok',
                                        (migrations._LOCK,))[0]['ok']
                self.assertFalse(locked)
        with db_mod.schema_guard_bypass(self.admin), self.admin.transaction() as observer:
            self.assertTrue(observer.query('SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok',
                                           (migrations._LOCK,))[0]['ok'])

    @unittest.skipUnless(HAVE_PSYCOPG, 'psycopg-specific transaction/round-trip contract')
    def test_savepoint_recovery_keeps_driver_transaction_usable(self):
        for driver in ('psycopg',):
            writer = db_mod.connect(self.cfg, driver=driver)
            try:
                with writer.transaction() as tx:
                    tx.execute('SAVEPOINT recovery')
                    with self.assertRaises(db_mod.DbError):
                        tx.query('SELECT missing_column')
                    tx.execute('ROLLBACK TO SAVEPOINT recovery')
                    tx.execute('RELEASE SAVEPOINT recovery')
                    tx.execute('UPDATE guard_writes SET n = 5')
            finally:
                writer.close()

    @unittest.skipUnless(HAVE_PSYCOPG, 'psycopg-specific transaction/round-trip contract')
    def test_guard_does_not_add_round_trips(self):
        writer = db_mod.connect(self.cfg, driver='psycopg')
        try:
            before = db_mod.TRACE.round_trips
            writer.execute('UPDATE guard_writes SET n = %s', (3,))
            self.assertEqual(db_mod.TRACE.round_trips - before, 1)
            before = db_mod.TRACE.round_trips
            with writer.transaction() as tx:
                tx.execute('UPDATE guard_writes SET n = %s', (4,))
                tx.execute('UPDATE guard_writes SET n = %s', (5,))
            self.assertEqual(db_mod.TRACE.round_trips - before, 2)
        finally:
            writer.close()

    def test_database_administration_exemption_is_scoped(self):
        with self.assertRaisesRegex(db_mod.DbError, 'only accepts'):
            db_mod.execute_without_statement_timeout(self.admin,
                                                     'UPDATE guard_writes SET n = 99')
        self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 0}])

    @unittest.skipUnless(HAVE_PSYCOPG, 'psycopg-specific transaction/round-trip contract')
    def test_savepoint_rollback_requires_fresh_compatibility_check(self):
        writer = db_mod.connect(self.cfg, driver='psycopg')
        try:
            with self.assertRaises(migrations.IncompatibleSchema):
                with writer.transaction() as tx:
                    tx.execute('SAVEPOINT before_guard')
                    tx.execute('UPDATE guard_writes SET n = 8')
                    tx.execute('ROLLBACK TO SAVEPOINT before_guard')
                    self.mark_unknown()
                    tx.execute('UPDATE guard_writes SET n = 9')
            self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 0}])
        finally:
            writer.close()

    def test_combined_guard_keeps_values_as_parameters(self):
        payload = "quote'; UPDATE guard_writes SET n = 99; -- $ameesh_writer_guard$ %s"
        rows = self.admin.query('INSERT INTO agent_registry (name) VALUES (%s) RETURNING name',
                                (payload,))
        self.assertEqual(rows, [{'name': payload}])
        self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 0}])

    def test_restricted_writer_only_needs_marker_read_permission(self):
        role = 't_guard_writer_' + uuid.uuid4().hex[:12]
        q = db_mod.quote_ident
        self.admin.execute('CREATE ROLE %s NOLOGIN' % q(role))
        def drop_role():
            with db_mod.schema_guard_bypass(self.admin):
                self.admin.execute('DROP OWNED BY %s' % q(role))
                self.admin.execute('DROP ROLE %s' % q(role))
        self.addCleanup(drop_role)
        self.admin.execute('GRANT USAGE ON SCHEMA %s TO %s' % (q(self.schema), q(role)))
        self.admin.execute('GRANT INSERT ON agent_registry TO %s' % q(role))
        for driver in DRIVERS:
            with self.subTest(driver=driver):
                writer = db_mod.connect(self.cfg, driver=driver)
                try:
                    with self.assertRaisesRegex(db_mod.DbError, 'permission denied'):
                        with writer.transaction() as tx:
                            with db_mod.schema_guard_bypass(writer):
                                tx.execute('SET LOCAL ROLE %s' % q(role))
                            tx.execute("INSERT INTO agent_registry VALUES ('refused')")
                    self.admin.execute('GRANT SELECT ON schema_non_additive_migrations TO %s' % q(role))
                    with writer.transaction() as tx:
                        with db_mod.schema_guard_bypass(writer):
                            tx.execute('SET LOCAL ROLE %s' % q(role))
                        tx.execute("INSERT INTO agent_registry VALUES ('accepted')")
                    self.assertFalse(self.admin.query(
                        "SELECT has_table_privilege(%s, 'schema_non_additive_migrations', "
                        "'INSERT,UPDATE,DELETE') AS p", (role,))[0]['p'])
                    self.mark_unknown()
                    with self.assertRaises(migrations.IncompatibleSchema):
                        with writer.transaction() as tx:
                            with db_mod.schema_guard_bypass(writer):
                                tx.execute('SET LOCAL ROLE %s' % q(role))
                            tx.execute("INSERT INTO agent_registry VALUES ('incompatible')")
                finally:
                    writer.close()
                with db_mod.schema_guard_bypass(self.admin):
                    self.admin.execute('DELETE FROM schema_non_additive_migrations')
                    self.admin.execute('DELETE FROM schema_migrations')
                    self.admin.execute('REVOKE SELECT ON schema_non_additive_migrations FROM %s' % q(role))
        self.assertEqual(self.admin.query('SELECT name FROM agent_registry'),
                         [{'name': 'accepted'}] * len(DRIVERS))

    def test_additive_ahead_is_accepted(self):
        self.mark_unknown(additive=True)
        for driver in DRIVERS:
            writer = db_mod.connect(self.cfg, driver=driver)
            try:
                writer.execute('UPDATE guard_writes SET n = 3')
                migrations.require_compatible_writer(writer)
            finally:
                writer.close()
        self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 3}])

    def test_probe_names_incompatible_migration(self):
        self.mark_unknown()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli.cmd_probe(self.cfg)
        self.assertEqual(result, 1)
        self.assertIn('9999_future', output.getvalue())
        self.assertIn('writes refused', output.getvalue())

    def test_known_version_with_different_content_is_refused(self):
        known = migrations.discover()[-1]
        with db_mod.schema_guard_bypass(self.admin):
            self.admin.execute('INSERT INTO schema_migrations VALUES (%s, %s, %s)',
                               (known.version, known.name, '0' * 64))
            self.admin.execute('INSERT INTO schema_non_additive_migrations VALUES (%s, %s, %s)',
                               (known.version, known.name, '0' * 64))
        with self.assertRaises(migrations.IncompatibleSchema):
            self.admin.execute('UPDATE guard_writes SET n = 9')

    def test_ordinary_doctor_names_incompatible_migration(self):
        from unittest import mock
        self.mark_unknown()
        output = io.StringIO()
        with mock.patch.object(cli, '_profile_audit', return_value=None), contextlib.redirect_stdout(output):
            result = cli.cmd_doctor(self.cfg, notify_test=False)
        self.assertEqual(result, 1)
        self.assertIn('9999_future', output.getvalue())

    def test_marker_additive_and_worker_records_non_additive_atomically(self):
        marker = next(m for m in migrations.discover() if m.version == 524)
        self.assertEqual(migrations.non_additive(marker), [])
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '0001_break.sql').write_text(
                '-- ameesh: non-additive\nUPDATE guard_writes SET n = 7;')
            migrations.migrate(self.admin, directory)
            rows = self.admin.query('SELECT version FROM schema_non_additive_migrations')
            self.assertEqual(rows, [{'version': 1}])
            migrations.require_compatible_writer(self.admin, directory)
            with self.assertRaises(migrations.IncompatibleSchema):
                self.admin.execute('UPDATE guard_writes SET n = 8')

    def test_failed_migration_rolls_back_marker_and_data(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '0001_break.sql').write_text(
                '-- ameesh: non-additive\nUPDATE guard_writes SET n = 7;')
            Path(directory, '0002_fail.sql').write_text('SELECT missing_column;')
            with self.assertRaises(db_mod.DbError):
                migrations.migrate(self.admin, directory)
        self.assertEqual(self.admin.query('SELECT n FROM guard_writes'), [{'n': 0}])
        self.assertEqual(self.admin.query('SELECT * FROM schema_non_additive_migrations'), [])
        self.assertEqual(self.admin.query('SELECT * FROM schema_migrations'), [])
