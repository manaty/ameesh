# SPDX-License-Identifier: AGPL-3.0-only
"""Connection retention, transaction isolation and capacity warning (#653)."""
import importlib.util
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest import mock

from .support import PgTestCase, REPO
from ameesh import db_capacity, notify
from ameesh.scoped_db import ScopedDb


class ScopedConnections(PgTestCase):
    def setUp(self):
        super().setUp()
        self.scoped = ScopedDb(self.cfg)
        self.db.execute('CREATE TABLE IF NOT EXISTS scoped_values (n integer)')
        self.db.execute('TRUNCATE scoped_values')

    def present(self, pid):
        return self.db.query('SELECT count(*)::int AS n FROM pg_stat_activity WHERE pid=%s',
                             (pid,))[0]['n']

    def test_session_released_before_external_work(self):
        pid = self.scoped.query('SELECT pg_backend_pid() AS pid')[0]['pid']
        self.assertEqual(self.present(pid), 0)
        self.scoped.execute('INSERT INTO scoped_values VALUES (1)')
        self.assertEqual(self.db.query('SELECT n FROM scoped_values'), [{'n': 1}])

    def test_transaction_keeps_session_and_rolls_back_as_a_unit(self):
        with self.assertRaisesRegex(ValueError, 'abort'):
            with self.scoped.transaction() as tx:
                pid = tx.query('SELECT pg_backend_pid() AS pid')[0]['pid']
                tx.execute('INSERT INTO scoped_values VALUES (1)')
                self.assertEqual(self.present(pid), 1)
                self.assertEqual(tx.query('SELECT n FROM scoped_values'), [{'n': 1}])
                self.assertEqual(self.db.query('SELECT n FROM scoped_values'), [])
                raise ValueError('abort')
        self.assertEqual(self.present(pid), 0)
        self.assertEqual(self.db.query('SELECT n FROM scoped_values'), [])

    def test_nested_transaction_rollback_retains_outer_session(self):
        if self.db.name == "psql":
            self.skipTest("psql nested contexts do not implement savepoints")
        with self.scoped.transaction() as tx:
            tx.execute('INSERT INTO scoped_values VALUES (1)')
            with self.assertRaises(ValueError):
                with tx.transaction() as nested:
                    nested.execute('INSERT INTO scoped_values VALUES (2)')
                    raise ValueError()
            self.assertEqual(tx.query('SELECT n FROM scoped_values'), [{'n': 1}])
        self.assertEqual(self.db.query('SELECT n FROM scoped_values'), [{'n': 1}])

    def test_concurrent_transactions_do_not_join_each_other(self):
        ready = threading.Barrier(2, timeout=15)
        errors, pids = [], []

        def run(value):
            try:
                with self.scoped.transaction() as tx:
                    pids.append(tx.query('SELECT pg_backend_pid() AS pid')[0]['pid'])
                    tx.execute('INSERT INTO scoped_values VALUES (%s)', (value,))
                    ready.wait()
                    self.assertEqual(tx.query('SELECT n FROM scoped_values'), [{'n': value}])
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=run, args=(value,)) for value in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(set(pids)), 2)
        self.assertTrue(all(self.present(pid) == 0 for pid in pids))

    def test_capacity_query_works_with_each_driver(self):
        reading = db_capacity.read(self.db)
        self.assertGreater(reading['maximum'], 0)
        self.assertGreaterEqual(reading['used'], 1)
        self.assertLessEqual(reading['idle'], reading['used'])


class CapacityWarning(unittest.TestCase):
    def test_boundary_and_default_push(self):
        for maximum, used, expected in ((100, 69, False), (100, 70, True),
                                        (300, 209, False), (300, 210, True),
                                        (300, 300, True)):
            with self.subTest(maximum=maximum, used=used), \
                    mock.patch.object(db_capacity, 'read', return_value={
                        'maximum': maximum, 'used': used, 'idle': used - 1}):
                alerts = db_capacity.alerts(None)
                self.assertEqual(bool(alerts), expected)
                if alerts:
                    self.assertEqual(alerts[0]['maximum'], maximum)
                    self.assertEqual(alerts[0]['value'], used)
        self.assertIn('db_connections_high', notify.DEFAULT_TYPES)
        self.assertIn('db_connections_high', notify.URGENT_TYPES)

    def test_mediated_has_no_sql_capacity_probe(self):
        db = mock.Mock(cfg=mock.Mock(backend='mediated'))
        self.assertIsNone(db_capacity.read(db))
        db.query.assert_not_called()


class OperatorRecipe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            'pgbouncer_host', Path(REPO) / 'deploy/pgbouncer-host.py')
        cls.recipe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.recipe)

    def test_secret_quoting_and_control_character_rejection(self):
        self.assertEqual(self.recipe.userlist_quote('p"a\\ss'), '"p""a\\ss"')
        for value in ('password\nextra', 'password\r', 'password\x00'):
            with self.assertRaises(ValueError):
                self.recipe.userlist_quote(value)

    def test_switch_backup_and_rollback_are_exact_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / '.config/ameesh/config.json'
            config.parent.mkdir(parents=True)
            original = b'{"dsn":"host=server password=fake-secret", "poll": 5}\n'
            config.write_bytes(original)
            state = home / '.local/state/pgbouncer'
            state.mkdir(parents=True)
            (state / 'socket.dsn').write_text('host=/private/socket port=6432')
            self.recipe.switch(home, state)
            self.assertEqual(json.loads(config.read_bytes())['poll'], 5)
            self.assertEqual(json.loads(config.read_bytes())['dsn'],
                             'host=/private/socket port=6432')
            backup = config.with_name('config.json.before-pgbouncer-653')
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.recipe.switch(home, state, rollback=True)
            self.assertEqual(config.read_bytes(), original)
            with self.assertRaises(FileExistsError):
                self.recipe.switch(home, state)
            self.assertEqual(config.read_bytes(), original)

    def test_private_directory_rejects_symlink_and_shared_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / 'shared'
            shared.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                self.recipe.private_dir(shared)
            link = root / 'link'
            link.symlink_to(shared)
            with self.assertRaises(ValueError):
                self.recipe.private_dir(link)
