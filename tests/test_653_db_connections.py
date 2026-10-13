# SPDX-License-Identifier: AGPL-3.0-only
"""Connection retention, transaction isolation and capacity warning (#653)."""
import importlib.util
import copy
import contextlib
from pathlib import Path
import json
import types
import tempfile
import threading
import time
import unittest
from unittest import mock

from .support import PgTestCase, REPO
from ameesh import balance, db as db_mod, db_capacity, exploitation, notify
from ameesh.runner import Runner
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
        # Second rendez-vous : les deux SELECT intra-transaction ont eu lieu avant
        # tout commit. Sans lui, un fil peut terminer (SELECT, commit, close) avant
        # que l'autre ne lise, et en READ COMMITTED ce dernier voit alors la ligne
        # committée de l'autre en plus de la sienne (rouge CI #814, 13/10).
        queried = threading.Barrier(2, timeout=15)
        errors, pids = [], []

        def run(value):
            try:
                with self.scoped.transaction() as tx:
                    pids.append(tx.query('SELECT pg_backend_pid() AS pid')[0]['pid'])
                    tx.execute('INSERT INTO scoped_values VALUES (%s)', (value,))
                    ready.wait()
                    self.assertEqual(tx.query('SELECT n FROM scoped_values'), [{'n': value}])
                    queried.wait()
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
        # Le backend quitte pg_stat_activity de façon asynchrone après close() :
        # on attend sa disparition (borné) au lieu de la lire une seule fois.
        closed = False
        for _ in range(100):
            if not any(self.present(pid) for pid in pids):
                closed = True
                break
            time.sleep(0.05)
        self.assertTrue(closed, "session toujours ouverte après la transaction")

    def test_capacity_query_works_with_each_driver(self):
        reading = db_capacity.read(self.db)
        self.assertGreater(reading['maximum'], 0)
        self.assertGreaterEqual(reading['used'], 1)
        self.assertLessEqual(reading['idle'], reading['used'])

    def test_runner_balance_pass_releases_session_before_provider_fetch(self):
        # Execute the runner boundary and the real balance.record path. A
        # rollback of runner.balance_once to db_mod.connect keeps its driver
        # open during fetch and makes this test fail, even with ScopedDb intact.
        opened = []
        original_connect = db_mod.connect

        def connect(cfg):
            driver = original_connect(cfg)
            driver.close = mock.Mock(wraps=driver.close)
            opened.append(driver)
            return driver

        source = mock.Mock(provider='deepseek')
        source.configured.return_value = True

        def fetch():
            self.assertTrue(opened, 'balance.record must query before HTTP')
            self.assertTrue(all(driver.close.called for driver in opened),
                            'runner retained an auxiliary session during HTTP')
            return [balance.BalanceReading('deepseek', 'USD', 42.0)]

        source.fetch.side_effect = fetch
        runner = Runner(self.cfg, self.db, once=True)
        with mock.patch('ameesh.db.connect', side_effect=connect):
            self.assertEqual(runner.balance_once([source], min_interval_s=1), 1)
        source.fetch.assert_called_once()

    def test_probe_failure_keeps_notifier_warning_without_repeated_raise(self):
        notifier = notify.Notifier(self.cfg, notify.parse_config({
            'default_human': 'human:operator', 'default': ['desktop'],
            'types': ['db_connections_high']}), dry_run=True, log=lambda _: None)
        high = {'used': 210, 'maximum': 300, 'idle': 200}
        with mock.patch.object(exploitation, 'alerts',
                               side_effect=lambda _cfg, db, **_: db_capacity.alerts(db)), \
                mock.patch.object(db_capacity, 'read', return_value=high):
            first = notifier.run_pass(self.db)
            self.assertEqual([r['event'] for r in first], ['raised'])
            previous = copy.deepcopy(notifier.state)
            with mock.patch.object(db_capacity, 'read', side_effect=db_mod.DbError('probe')):
                with self.assertRaises(db_mod.DbError):
                    notifier.run_pass(self.db)
            self.assertEqual(notifier.state, previous)
            self.assertEqual(notifier.run_pass(self.db), [])
            with mock.patch.object(db_capacity, 'read', return_value={
                    'used': 209, 'maximum': 300, 'idle': 200}):
                resolved = notifier.run_pass(self.db)
                self.assertEqual([r['event'] for r in resolved], ['resolved'])


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

    def test_host_user_guard_runs_before_every_action_mutation(self):
        for action in ('install', 'switch', 'rollback', 'measure', 'verify'):
            for host, user, uid, effective, home_ok in (
                    ('wrong-host', 'ameesh', 1001, 1001, True),
                    ('ameesh-agents-1', 'wrong-user', 1001, 1001, True),
                    ('ameesh-agents-1', 'root', 0, 0, True),
                    ('ameesh-agents-1', 'ameesh', 1001, 0, True),
                    ('ameesh-agents-1', 'ameesh', 1001, 1001, False)):
                with self.subTest(action=action, host=host, user=user, uid=uid,
                                  effective=effective, home_ok=home_ok), \
                        mock.patch('sys.argv', ['recipe', action]), \
                        mock.patch.object(self.recipe.socket, 'gethostname', return_value=host), \
                        mock.patch.object(self.recipe.os, 'getuid', return_value=uid), \
                        mock.patch.object(self.recipe.os, 'geteuid', return_value=effective), \
                        mock.patch.object(self.recipe.pwd, 'getpwuid', return_value=types.SimpleNamespace(
                            pw_name=user, pw_dir='/home/ameesh')), \
                        mock.patch.object(Path, 'home', return_value=Path(
                            '/home/ameesh' if home_ok else '/wrong-home')), \
                        mock.patch.object(self.recipe, 'private_dir') as mutation:
                    with self.assertRaises(ValueError):
                        self.recipe.main()
                    mutation.assert_not_called()
        for host in ('ameesh-agents-1', 'ameesh-agents-2'):
            with mock.patch.object(self.recipe.socket, 'gethostname', return_value=host), \
                    mock.patch.object(self.recipe.os, 'getuid', return_value=1001), \
                    mock.patch.object(self.recipe.os, 'geteuid', return_value=1001), \
                    mock.patch.object(self.recipe.pwd, 'getpwuid', return_value=types.SimpleNamespace(
                        pw_name='ameesh', pw_dir='/home/ameesh')), \
                    mock.patch.object(Path, 'home', return_value=Path('/home/ameesh')):
                self.assertEqual(self.recipe.approved_home(), Path('/home/ameesh'))

    def test_socket_dsn_removes_upstream_tls_and_preserves_startup_options(self):
        try:
            import psycopg
        except ImportError:
            self.skipTest('operator recipe requires psycopg; covered by the psycopg job')
        upstream = psycopg.conninfo.make_conninfo(
            host='upstream', hostaddr='192.0.2.1', port='5432', user='ameesh', dbname='ameesh',
            sslmode='require', sslcert='/private/cert',
            sslkey='/private/key', sslrootcert='/private/root', channel_binding='require',
            options='-c search_path=test_schema -c statement_timeout=12345')
        local = psycopg.conninfo.conninfo_to_dict(
            self.recipe.socket_conninfo(upstream, Path('/private/socket')))
        self.assertEqual(local['host'], '/private/socket')
        self.assertEqual(local['sslmode'], 'disable')
        self.assertEqual(local['options'], '-c search_path=test_schema -c statement_timeout=12345')
        if psycopg.pq.version() >= 170000:
            self.assertEqual(local['sslnegotiation'], 'postgres')
        else:
            self.assertNotIn('sslnegotiation', local)
        self.assertFalse(set(local) & {'sslcert', 'sslkey',
                                      'sslrootcert', 'hostaddr', 'channel_binding'})

    def test_socket_dsn_overrides_direct_negotiation_from_environment_and_service(self):
        try:
            import psycopg
        except ImportError:
            self.skipTest('operator recipe requires psycopg; covered by the psycopg job')
        if psycopg.pq.version() < 170000:
            self.skipTest('direct negotiation only exists in libpq 17+')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = root / 'pg_service.conf'
            service.write_text('[upstream]\nsslnegotiation=direct\n')
            # A nonexistent Unix socket gives a deterministic transport error.
            # libpq must reach that stage instead of rejecting the TLS settings.
            for upstream in ('dbname=test', 'service=upstream dbname=test'):
                with self.subTest(upstream=upstream), mock.patch.dict(
                        'os.environ', {'PGSSLNEGOTIATION': 'direct',
                                       'PGSERVICEFILE': str(service)}):
                    local = self.recipe.socket_conninfo(upstream, root / 'missing')
                    with self.assertRaises(psycopg.OperationalError) as caught:
                        psycopg.connect(local, connect_timeout=1)
                    self.assertNotIn('weak sslmode', str(caught.exception))
                    self.assertIn(str(root / 'missing'), str(caught.exception))

    def test_socket_dsn_supports_both_libpq_version_branches(self):
        try:
            import psycopg
        except ImportError:
            self.skipTest('operator recipe requires psycopg')
        real_make = psycopg.conninfo.make_conninfo
        for version in (160000, 170000):
            def checked_make(**params):
                if version < 170000 and 'sslnegotiation' in params:
                    raise psycopg.ProgrammingError('invalid connection option "sslnegotiation"')
                return real_make(**params)
            with self.subTest(version=version), \
                    mock.patch.object(psycopg.pq, 'version', return_value=version), \
                    mock.patch.object(psycopg.conninfo, 'make_conninfo', side_effect=checked_make):
                local = psycopg.conninfo.conninfo_to_dict(self.recipe.socket_conninfo(
                    'dbname=test sslmode=require options=\'-c statement_timeout=12345\'',
                    Path('/private/socket')))
                self.assertEqual(local['sslmode'], 'disable')
                self.assertEqual(local['options'], '-c statement_timeout=12345')
                self.assertEqual(local.get('sslnegotiation'),
                                 'postgres' if version >= 170000 else None)

    def test_failed_install_can_be_retried_without_removing_preexisting_files(self):
        try:
            import psycopg
        except ImportError:
            self.skipTest('operator recipe requires psycopg')
        from ameesh import config as config_mod
        for failure in ('dsn', 'cleanup', 'version', 'write', 'verify', 'start'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, \
                    mock.patch.dict('os.environ', {'TMPDIR': directory}):
                home = Path(directory)
                original = home / '.config/ameesh/config.json'
                original.parent.mkdir(parents=True)
                raw = b'{"dsn":"dbname=test"}'
                original.write_bytes(raw)
                state = home / '.local/state/pgbouncer'
                config = home / '.config/pgbouncer/pgbouncer.ini'
                binary = home / '.local/bin/pgbouncer'
                unit = home / '.config/systemd/user/ameesh-pgbouncer.service'
                settings = config.parent
                settings.mkdir(parents=True, mode=0o700)
                sentinel = settings / 'unrelated'
                sentinel.write_text('keep')
                conn = mock.MagicMock()
                conn.__enter__.return_value = conn
                conn.info.get_parameters.return_value = dict(
                    dbname='test', user='ameesh', host='upstream', port='5432', sslmode='require')
                conn.pgconn.password = b'fake-secret'
                conn.pgconn.ssl_in_use = True

                real_compile = self.recipe.compile_binary
                real_temporary = tempfile.TemporaryDirectory
                cleanup_failed = False

                @contextlib.contextmanager
                def temporary_with_failed_cleanup(*args, **kwargs):
                    nonlocal cleanup_failed
                    with real_temporary(*args, **kwargs) as directory:
                        yield directory
                    if not cleanup_failed:
                        cleanup_failed = True
                        raise OSError('simulated TemporaryDirectory cleanup failure')

                def extract_fake(directory, **kwargs):
                    source = Path(directory) / f'pgbouncer-{self.recipe.VERSION}'
                    source.mkdir()
                    (source / 'pgbouncer').write_bytes(b'fake pinned binary')

                def compile_fake(_home, create):
                    if failure == 'cleanup':
                        # Exercise the real compile function, including binary
                        # creation followed by an exception from context exit.
                        with mock.patch.object(self.recipe.tempfile, 'TemporaryDirectory',
                                               side_effect=temporary_with_failed_cleanup), \
                                mock.patch.object(self.recipe, 'urlopen') as download, \
                                mock.patch.object(self.recipe.hashlib, 'sha256') as digest, \
                                mock.patch.object(self.recipe.tarfile, 'open') as archive:
                            download.return_value.__enter__.return_value.read.return_value = b'archive'
                            digest.return_value.hexdigest.return_value = self.recipe.SHA256
                            archive.return_value.__enter__.return_value.extractall.side_effect = extract_fake
                            return real_compile(_home, create)
                    binary.parent.mkdir(parents=True, exist_ok=True)
                    create(binary, b'fake pinned binary', 0o755)

                real_write = self.recipe.write_new
                def fail_write(path, content, mode=0o600):
                    if path == state / 'socket.dsn':
                        raise OSError('simulated write failure')
                    return real_write(path, content, mode)

                def fail_command(argv):
                    if (failure == 'verify' and argv[0] == 'systemd-analyze') or (
                            failure == 'start' and 'start' in argv):
                        raise RuntimeError('simulated command failure')

                with mock.patch.object(config_mod, 'load', return_value=types.SimpleNamespace(
                        backend='postgres', dsn='dbname=test')), \
                        mock.patch.object(psycopg, 'connect', return_value=conn), \
                        mock.patch.object(self.recipe, 'compile_binary', side_effect=compile_fake) as build, \
                        mock.patch.object(self.recipe.subprocess, 'run', return_value=types.SimpleNamespace(
                            stdout='wrong' if failure == 'version' else 'PgBouncer 1.26.0')) as version, \
                        mock.patch.object(self.recipe, 'run', side_effect=fail_command) as command:
                    with mock.patch.object(self.recipe, 'socket_conninfo',
                            side_effect=ValueError('bad option') if failure == 'dsn' else None,
                            return_value='host=/private/socket sslmode=disable'), \
                            mock.patch.object(self.recipe, 'write_new',
                                side_effect=fail_write if failure == 'write' else real_write):
                        with self.assertRaises((ValueError, OSError, RuntimeError)):
                            self.recipe.install(home, state, config)
                    for artifact in (binary, config, settings / 'userlist.txt',
                                     state / 'socket.dsn', unit):
                        self.assertFalse(artifact.exists(), artifact)
                    self.assertEqual(original.read_bytes(), raw)
                    self.assertEqual(sentinel.read_text(), 'keep')
                    if failure == 'dsn':
                        build.assert_not_called()
                    if failure == 'cleanup':
                        self.assertTrue(cleanup_failed)
                        self.assertFalse(any(call.args[0][0] == str(binary)
                                             for call in version.call_args_list))
                    if failure == 'start':
                        self.assertIn(mock.call(['systemctl', '--user', 'stop',
                                                'ameesh-pgbouncer.service']), command.call_args_list)
                    version.return_value.stdout = 'PgBouncer 1.26.0'
                    command.side_effect = None
                    self.recipe.install(home, state, config)
                    self.assertTrue(binary.exists())
                    self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
                    self.assertTrue(unit.exists())
                    self.assertEqual(original.read_bytes(), raw)

    def test_failed_exclusive_write_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'new-file'
            with mock.patch.object(self.recipe.os, 'fsync', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    self.recipe.write_new(path, b'partial')
            self.assertFalse(path.exists())
            self.recipe.write_new(path, b'retry')
            self.assertEqual(path.read_bytes(), b'retry')

    def test_approved_home_compares_and_returns_canonical_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_home = root / 'real-home'
            real_home.mkdir()
            alias = root / 'alias-home'
            alias.symlink_to(real_home, target_is_directory=True)
            with mock.patch.object(self.recipe.socket, 'gethostname', return_value='ameesh-agents-1'), \
                    mock.patch.object(self.recipe.os, 'getuid', return_value=1001), \
                    mock.patch.object(self.recipe.os, 'geteuid', return_value=1001), \
                    mock.patch.object(self.recipe.pwd, 'getpwuid', return_value=types.SimpleNamespace(
                        pw_name='ameesh', pw_dir=str(alias))), \
                    mock.patch.object(Path, 'home', return_value=real_home):
                self.assertEqual(self.recipe.approved_home(), real_home)

    def test_preexisting_binary_is_refused_without_execution_or_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            binary = home / '.local/bin/pgbouncer'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\necho "PgBouncer 1.26.0"\n')
            binary.chmod(0o755)
            state = home / '.local/state/pgbouncer'
            config = home / '.config/pgbouncer/pgbouncer.ini'
            with mock.patch.object(self.recipe.subprocess, 'run') as execute, \
                    mock.patch.object(self.recipe, 'private_dir') as mutation:
                with self.assertRaisesRegex(ValueError, 'provenance'):
                    self.recipe.install(home, state, config)
                execute.assert_not_called()
                mutation.assert_not_called()
                self.assertFalse(config.exists())

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
