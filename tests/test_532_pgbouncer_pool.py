# SPDX-License-Identifier: AGPL-3.0-only
"""Local PgBouncer pool sizing and saturation warning (#532)."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from .support import REPO
from ameesh import config as config_mod, db as db_mod, exploitation, notify, pgbouncer_local


def recipe_module():
    spec = importlib.util.spec_from_file_location(
        'pgbouncer_host_532', Path(REPO) / 'deploy/pgbouncer-host.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_console(pools, lists=None, config=None):
    """A minimal admin console: SHOW POOLS, SHOW LISTS, SHOW CONFIG."""
    answers = {'SHOW POOLS': pools, 'SHOW LISTS': lists or [], 'SHOW CONFIG': config or []}

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql):
            return types.SimpleNamespace(fetchall=lambda: answers[sql])

    return Conn()


class RecipePoolSizing(unittest.TestCase):
    """The generator must keep the limits applied by hand on 2026-10-13, or a
    redeployment lowers them back to the saturated values (#532)."""

    @classmethod
    def setUpClass(cls):
        cls.recipe = recipe_module()

    def test_generated_config_keeps_the_raised_limits(self):
        ini = self.recipe.pool_ini('ameesh', '10.77.0.1', '5432',
                                   socket_dir=Path('/private/socket'),
                                   auth_file=Path('/private/userlist.txt'),
                                   admin_user='ameesh', tls='require', host='ameesh-agents-1')
        self.assertIn('pool_mode = session', ini)
        self.assertIn('unix_socket_dir = /private/socket', ini)
        self.assertIn('auth_file = /private/userlist.txt', ini)
        self.assertIn('admin_users = ameesh', ini)
        self.assertIn('server_tls_sslmode = require', ini)
        self.assertIn('max_client_conn = 250', ini)
        self.assertIn('default_pool_size = 64', ini)
        self.assertIn('reserve_pool_size = 16', ini)
        self.assertIn('max_db_connections = 80', ini)
        # the reserve pool is usable: the database ceiling must allow both
        self.assertGreaterEqual(self.recipe.MAX_DB_CONNECTIONS,
                                self.recipe.DEFAULT_POOL_SIZE
                                + self.recipe.RESERVE_POOL_SIZE)

    def test_four_host_budget_preserves_existing_pools_and_bounds_new_host(self):
        limits = self.recipe.POOL_LIMITS
        self.assertEqual(set(limits), {'pc-smichea', 'ameesh-agents-1',
                                      'ameesh-agents-2', 'ameesh-agents-3'})
        self.assertLess(sum(cap for _, _, cap in limits.values()), 300)
        for host, (default, reserve, cap) in limits.items():
            with self.subTest(host=host):
                self.assertLessEqual(default + reserve, cap)
                with mock.patch.object(self.recipe.socket, 'gethostname', return_value=host):
                    ini = self.recipe.pool_ini('ameesh', '10.77.0.1', '5432',
                        socket_dir=Path('/private/socket'),
                        auth_file=Path('/private/userlist.txt'), admin_user='ameesh', tls='require')
                self.assertIn('max_db_connections = %s' % cap, ini)
                self.assertIn('default_pool_size = %s' % default, ini)
                self.assertIn('reserve_pool_size = %s' % reserve, ini)
        self.assertEqual(limits['ameesh-agents-3'], (32, 8, 40))


def every_startup_setting():
    """All the `-c` settings `db.startup_options` can send, read-only included."""
    cfg = config_mod.Config(dsn='postgresql://u@h/db', schema='ameesh_test',
                            statement_timeout_ms=30000, read_only=True)
    return db_mod.startup_options(cfg)


class RecipeTracksSessionSettings(unittest.TestCase):
    """#3745: pgbouncer refuses a session carrying a `-c` setting it does not
    track. `-c jit=off` (#3503) was refused by the pool of pc-smichea, whose
    generated `track_extra_parameters` stopped at statement_timeout."""

    @classmethod
    def setUpClass(cls):
        cls.recipe = recipe_module()

    def generated_tracking(self):
        ini = self.recipe.pool_ini('ameesh', '10.77.0.1', '5432',
                                   socket_dir=Path('/private/socket'),
                                   auth_file=Path('/private/userlist.txt'),
                                   admin_user='ameesh', tls='require')
        lines = [line for line in ini.splitlines()
                 if line.startswith('track_extra_parameters = ')]
        self.assertEqual(len(lines), 1)
        return lines[0].partition(' = ')[2]

    def test_generated_config_tracks_every_ameesh_session_setting(self):
        options = every_startup_setting()
        self.assertIn('jit', pgbouncer_local.option_names(options))
        self.assertEqual(pgbouncer_local.startup_problems(
            options, {'track_extra_parameters': self.generated_tracking()}), [])

    def test_the_former_tracking_refused_jit(self):
        self.assertEqual(pgbouncer_local.startup_problems(
            every_startup_setting(),
            {'track_extra_parameters': 'search_path,statement_timeout'}),
            ['jit refused (unsupported startup parameter)'])


class StartupProblems(unittest.TestCase):
    def test_option_names_reads_both_spellings(self):
        self.assertEqual(pgbouncer_local.option_names(
            '-c search_path="a b" -cstatement_timeout=5 --jit=off'),
            ['search_path', 'statement_timeout', 'jit'])
        self.assertEqual(pgbouncer_local.option_names(''), [])

    def test_default_tracking_is_case_insensitive(self):
        self.assertEqual(pgbouncer_local.startup_problems(
            '-c default_transaction_read_only=on -c TimeZone=UTC',
            {'track_extra_parameters': ''}), [])
        self.assertEqual(pgbouncer_local.startup_problems(
            '-c JIT=off', {'track_extra_parameters': 'search_path, jit'}), [])

    def test_an_ignored_setting_is_never_applied(self):
        for ignored in ('jit', 'extra_float_digits, options'):
            self.assertEqual(pgbouncer_local.startup_problems(
                '-c jit=off', {'ignore_startup_parameters': ignored}),
                ['jit ignored (never applied to the session)'])


class StartupLines(unittest.TestCase):
    """`doctor` names a setting the running pool would refuse (#3745)."""

    def setUp(self):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest('requires psycopg')
        self.cfg = config_mod.Config(dsn='host=/private/socket port=6432 dbname=ameesh',
                                     schema='public', statement_timeout_ms=30000)

    def lines(self, config=None, error=None):
        def connect(*args, **kwargs):
            if error is not None:
                raise error
            return fake_console([], config=config)
        pgbouncer_local._PROBE = None
        self.addCleanup(setattr, pgbouncer_local, '_PROBE', None)
        with mock.patch.object(pgbouncer_local, 'admin_dsn', return_value='console'), \
                mock.patch.dict(os.environ, {'PGOPTIONS': ''}), \
                mock.patch('psycopg.connect', side_effect=connect):
            return pgbouncer_local.startup_lines(self.cfg)

    def test_no_local_pool_checks_nothing(self):
        with mock.patch.object(pgbouncer_local, 'admin_dsn', return_value=None):
            self.assertEqual(pgbouncer_local.startup_lines(self.cfg), (True, []))

    def test_running_tracking_without_jit_is_a_failure(self):
        ok, lines = self.lines([{'key': 'track_extra_parameters',
                                 'value': 'search_path,statement_timeout'}])
        self.assertFalse(ok)
        self.assertIn('jit refused', lines[0])
        self.assertIn('RESTART', lines[0])

    def test_running_tracking_with_jit_passes(self):
        ok, lines = self.lines([{'key': 'track_extra_parameters',
                                 'value': 'search_path,statement_timeout,jit'}])
        self.assertTrue(ok)
        self.assertEqual(lines, ['startup options accepted (statement_timeout, jit)'])

    def test_an_unreadable_console_is_not_a_verdict(self):
        ok, lines = self.lines(error=OSError('refused'))
        self.assertTrue(ok)
        self.assertIn('not checked', lines[0])


class ProbeNamesThePoolRefusal(unittest.TestCase):
    """`doctor --probe` guards the update of a host (deploy/mise-a-jour): a
    pool refusal is not a missing migration (#3745)."""

    def probe(self, message):
        import contextlib
        import io
        from ameesh import cli
        out = io.StringIO()
        with mock.patch.object(db_mod, 'connect', side_effect=db_mod.Unavailable(message)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_probe(config_mod.Config(dsn='host=/s')), 1)
        return out.getvalue()

    def test_refused_setting_points_at_the_pool(self):
        line = self.probe('psycopg: FATAL: unsupported startup parameter in options: jit')
        self.assertIn('track_extra_parameters', line)
        self.assertIn('deploy/pgbouncer-host.md', line)

    def test_other_failures_are_unchanged(self):
        self.assertNotIn('pgbouncer', self.probe('psycopg: connection refused'))


class DeclaredSocketDir(unittest.TestCase):
    def test_reads_whole_lines_only(self):
        with tempfile.TemporaryDirectory() as directory:
            ini = Path(directory) / 'pgbouncer.ini'
            ini.write_text('; unix_socket_dir = /ignored\n'
                           '# unix_socket_dir = /ignored\n'
                           '\n'
                           '[pgbouncer]\n'
                           'listen_port = 6432\n'
                           'unix_socket_dir = /tmp/a#b\n')
            self.assertEqual(pgbouncer_local.declared_socket_dir(ini), '/tmp/a#b')
            self.assertIsNone(pgbouncer_local.declared_socket_dir(Path(directory) / 'none'))
            ini.write_text('[pgbouncer]\nunix_socket_dir =\n')
            self.assertIsNone(pgbouncer_local.declared_socket_dir(ini))


class AdminDsn(unittest.TestCase):
    def setUp(self):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest('requires psycopg')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def home(self, declared=None):
        ini = self.root / pgbouncer_local.CONFIG_PATH
        ini.parent.mkdir(parents=True, exist_ok=True)
        if declared is not None:
            ini.write_text('[pgbouncer]\nunix_socket_dir = %s\n' % declared)
        return self.root

    def admin(self, dsn, declared=None, backend='pg'):
        cfg = types.SimpleNamespace(dsn=dsn, backend=backend)
        with mock.patch.object(Path, 'home', return_value=self.home(declared)):
            return pgbouncer_local.admin_dsn(cfg)

    def test_tcp_dsn_is_never_probed(self):
        self.assertIsNone(self.admin('host=10.77.0.1 port=5432 dbname=ameesh',
                                     declared=self.root / 'socket'))

    def test_socket_dsn_without_a_matching_config_is_not_probed(self):
        socket = str(self.root / 'socket')
        self.assertIsNone(self.admin('host=%s port=6432 dbname=ameesh' % socket))
        self.assertIsNone(self.admin('host=%s port=6432 dbname=ameesh' % socket,
                                     declared=self.root / 'other'))

    def test_matching_socket_returns_the_admin_console(self):
        import psycopg
        socket = str(self.root / 'socket')
        dsn = 'host=%s hostaddr=192.0.2.1 port=6432 user=ameesh dbname=ameesh ' \
              "sslmode=disable options='-c statement_timeout=30000'" % socket
        params = psycopg.conninfo.conninfo_to_dict(self.admin(dsn, declared=socket))
        self.assertEqual(params['dbname'], 'pgbouncer')
        self.assertEqual(params['host'], socket)
        self.assertEqual(params['port'], '6432')
        self.assertEqual(params['user'], 'ameesh')
        self.assertNotIn('hostaddr', params)
        self.assertEqual(params.get('options', ''), '')

    def test_mediated_config_has_no_local_pool(self):
        self.assertIsNone(self.admin('host=%s port=6432' % (self.root / 'socket'),
                                     declared=self.root / 'socket', backend='mediated'))

    def test_missing_psycopg_is_not_an_error(self):
        with mock.patch.dict(sys.modules, {'psycopg': None}):
            self.assertIsNone(self.admin('host=%s port=6432' % (self.root / 'socket'),
                                         declared=self.root / 'socket'))


class ReadPool(unittest.TestCase):
    def setUp(self):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest('requires psycopg')

    def test_aggregates_pools_and_ignores_the_console_pool(self):
        pools = [
            dict(database='ameesh', cl_active=40, cl_waiting=0, sv_active=38,
                 sv_idle=2, maxwait=0),
            dict(database='other', cl_active=2, cl_waiting=3, sv_active=1,
                 sv_idle=4, maxwait=7),
            dict(database='pgbouncer', cl_active=1, cl_waiting=0, sv_active=0,
                 sv_idle=0, maxwait=0),
        ]
        lists = [dict(list='used_clients', items=45), dict(list='free_clients', items=205)]
        config = [dict(key='max_client_conn', value='250')]
        with mock.patch('psycopg.connect', return_value=fake_console(pools, lists, config)):
            reading = pgbouncer_local.read('host=/private/socket dbname=pgbouncer')
        # N1 : `free_clients` (objets clients en cache) n'entre pas dans la
        # lecture ; le plafond est celui de SHOW CONFIG.
        self.assertEqual(reading, dict(waiting=3, maxwait_s=7, clients_active=42,
                                       servers_active=39, servers_idle=6,
                                       clients_used=45, clients_max=250,
                                       track_extra_parameters=None,
                                       ignore_startup_parameters=None))

    def test_unreadable_console_is_not_a_healthy_pool(self):
        with mock.patch('psycopg.connect', side_effect=RuntimeError('refused')):
            with self.assertRaises(db_mod.Unavailable):
                pgbouncer_local.read('host=/private/socket dbname=pgbouncer')

    def test_ill_typed_console_answer_is_not_a_reading(self):
        pools = [dict(database='ameesh', cl_active=0, cl_waiting='many', sv_active=0,
                      sv_idle=0, maxwait=0)]
        with mock.patch('psycopg.connect', return_value=fake_console(pools)):
            with self.assertRaises(db_mod.Unavailable):
                pgbouncer_local.read('host=/private/socket dbname=pgbouncer')


class Pressured(unittest.TestCase):
    def test_a_real_queue_is_required(self):
        reading = dict(waiting=0, maxwait_s=0, clients_active=0, servers_active=0,
                       servers_idle=0, clients_used=None, clients_max=None)
        for waiting, maxwait, expected in ((0, 0, False), (0, 9, False),
                                           (1, 0, False), (1, 1, True),
                                           (3, 30, True)):
            with self.subTest(waiting=waiting, maxwait=maxwait):
                self.assertEqual(
                    pgbouncer_local.pressured(dict(reading, waiting=waiting,
                                                   maxwait_s=maxwait)), expected)

    def test_line_names_the_queue_and_the_unknown_slots(self):
        text = pgbouncer_local.line(dict(waiting=60, maxwait_s=30, clients_active=40,
                                         servers_active=40, servers_idle=0,
                                         clients_used=None, clients_max=None))
        self.assertIn('60 client(s) waiting', text)
        self.assertIn('oldest 30s', text)
        self.assertIn('client slots unknown', text)

    def test_line_reads_the_real_client_ceiling(self):
        # N1 : des clients utilisés sur le plafond de SHOW CONFIG, pas un
        # prétendu ratio avec les objets clients libres.
        text = pgbouncer_local.line(dict(waiting=60, maxwait_s=30, clients_active=40,
                                         servers_active=40, servers_idle=0,
                                         clients_used=100, clients_max=250))
        self.assertIn('100/250 client slots used', text)


class PoolAlerts(unittest.TestCase):
    cfg = types.SimpleNamespace(dsn='host=/private/socket dbname=ameesh', backend='pg',
                                host='pc-smichea')
    saturated = dict(waiting=60, maxwait_s=30, clients_active=40, servers_active=40,
                     servers_idle=0, clients_used=100, clients_max=250)

    def db(self):
        return types.SimpleNamespace(cfg=self.cfg)

    def test_no_probe_when_the_dsn_is_not_a_local_pool(self):
        with mock.patch.object(pgbouncer_local, 'admin_dsn', return_value=None), \
                mock.patch.object(pgbouncer_local, 'read') as probe:
            self.assertEqual(pgbouncer_local.alerts(self.db()), [])
            probe.assert_not_called()

    def test_saturated_pool_raises_one_urgent_alert(self):
        with mock.patch.object(pgbouncer_local, 'admin_dsn', return_value='admin'), \
                mock.patch.object(pgbouncer_local, 'read',
                                  return_value=dict(self.saturated)):
            alerts = pgbouncer_local.alerts(self.db())
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert['type'], 'pgbouncer_pool_waiting')
        self.assertEqual(alert['value'], 60)
        self.assertEqual(alert['threshold'], 1)
        self.assertEqual(alert['host'], 'pc-smichea')
        self.assertIn('60 client(s) waiting', alert['detail'])
        # the notifier dedupes on this key: a lasting queue is one alert
        self.assertEqual(exploitation.alert_key(alert),
                         exploitation.alert_key(dict(alert, value=61, detail='other')))

    def test_probe_reads_from_the_configuration_alone(self):
        # B1 : le repli sans base lit la même console depuis `cfg` seule.
        with mock.patch.object(pgbouncer_local, 'admin_dsn', return_value='admin'), \
                mock.patch.object(pgbouncer_local, 'read',
                                  return_value=dict(self.saturated)):
            alerts = pgbouncer_local.probe(self.cfg)
        self.assertEqual([a['type'] for a in alerts], ['pgbouncer_pool_waiting'])

    def test_a_healthy_pool_raises_nothing(self):
        healthy = dict(self.saturated, waiting=0, maxwait_s=0)
        with mock.patch.object(pgbouncer_local, 'admin_dsn', return_value='admin'), \
                mock.patch.object(pgbouncer_local, 'read', return_value=healthy):
            self.assertEqual(pgbouncer_local.alerts(self.db()), [])

    def test_an_unreadable_console_never_resolves_silently(self):
        with mock.patch.object(pgbouncer_local, 'admin_dsn', return_value='admin'), \
                mock.patch.object(pgbouncer_local, 'read',
                                  side_effect=db_mod.Unavailable('refused')):
            with self.assertRaises(db_mod.Unavailable):
                pgbouncer_local.alerts(self.db())


class RecordingSender:
    def __init__(self, sent):
        self.sent = sent

    def send(self, channel, message):
        self.sent.append((channel.name, message.title, message.body))


class LocalFallback(unittest.TestCase):
    """B1 (#532) : l'alerte survit au refus qu'elle signale. La console se lit
    depuis `cfg` seule, puis l'alerte est routée et poussée alors que la
    connexion ordinaire — celle qui passe par ce même pool — échoue."""

    def setUp(self):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest('requires psycopg')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_dir = directory.name
        self.sent = []
        self.logs = []

    def cfg(self):
        return types.SimpleNamespace(dsn='host=/private/socket dbname=ameesh',
                                     backend='pg', host='pc-smichea', canon=None,
                                     state_dir=self.state_dir)

    def ncfg(self):
        return notify.parse_config({'default': ['desktop'],
                                    'default_human': 'human:proprio'})

    def console(self, waiting=60, maxwait=30, used=100, cap=250):
        pools = [dict(database='ameesh', cl_active=40, cl_waiting=waiting,
                      sv_active=40, sv_idle=0, maxwait=maxwait)]
        return fake_console(pools, [dict(list='used_clients', items=used)],
                            [dict(key='max_client_conn', value=str(cap))])

    def state_path(self):
        return os.path.join(self.state_dir, 'notify', 'state.json')

    def notifier(self):
        return notify.Notifier(self.cfg(), self.ncfg(),
                               sender=RecordingSender(self.sent),
                               log=self.logs.append, clock=lambda: 100.0)

    def queued(self):
        return mock.patch.object(pgbouncer_local, 'admin_dsn', return_value='admin'), \
            mock.patch('psycopg.connect', return_value=self.console())

    def test_the_real_notify_path_pushes_while_the_database_is_refused(self):
        args = notify.build_parser().parse_args(['--once'])
        refused = db_mod.Unavailable('no more connections allowed (max_client_conn)')
        admin, connect = self.queued()
        with admin, connect, \
                mock.patch.object(notify.db_mod, 'connect', side_effect=refused), \
                mock.patch.object(notify, 'Sender',
                                  lambda **kw: RecordingSender(self.sent)):
            # la base refuse la connexion : la passe ordinaire échoue, comme
            # lors de l'incident — et l'alerte est déjà partie.
            with self.assertRaises(db_mod.Unavailable):
                notify.cmd_notify(self.cfg(), self.ncfg(), args)
        self.assertEqual(len(self.sent), 1)
        channel, title, body = self.sent[0]
        self.assertEqual(channel, 'desktop')
        self.assertIn('local PgBouncer clients waiting', title)
        self.assertIn('60 client(s) waiting', body)
        self.assertIn('100/250 client slots used', body)
        with open(self.state_path(), encoding='utf-8') as fh:
            state = json.load(fh)
        [entry] = list(state['active'].values())
        self.assertEqual(entry['alert']['type'], 'pgbouncer_pool_waiting')
        self.assertTrue(all(st['ok'] for st in entry['channels'].values()))

    def test_a_database_outage_never_resolves_the_other_alerts(self):
        other = {'alert': {'type': 'stopped_with_mail', 'agent': 'ouvrier',
                           'detail': 'stopped (bail expiré) with 1 unread message(s)'},
                 'raised_ts': 1.0, 'human': 'human:proprio', 'source': 'agent',
                 'status': 'envoi',
                 'channels': {'desktop': {'ok': True, 'attempts': 1, 'error': None}}}
        state = notify.empty_state()
        state['active']['stopped_with_mail'] = other
        notify.save_state(self.state_path(), state)
        args = notify.build_parser().parse_args(['--once'])
        admin, connect = self.queued()
        with admin, connect, \
                mock.patch.object(notify.db_mod, 'connect',
                                  side_effect=db_mod.Unavailable('refused')), \
                mock.patch.object(notify, 'Sender',
                                  lambda **kw: RecordingSender(self.sent)):
            with self.assertRaises(db_mod.Unavailable):
                notify.cmd_notify(self.cfg(), self.ncfg(), args)
        with open(self.state_path(), encoding='utf-8') as fh:
            reloaded = json.load(fh)
        self.assertEqual(reloaded['active']['stopped_with_mail'], other)
        self.assertEqual([r for r in self.sent if 'stopped' in r[1].lower()], [])

    def test_an_unreadable_console_keeps_the_local_alert(self):
        notifier = self.notifier()
        admin, connect = self.queued()
        with admin, connect:
            notifier.run_local_pass()
        self.assertEqual(len(self.sent), 1)
        with mock.patch.object(pgbouncer_local, 'probe',
                               side_effect=db_mod.Unavailable('console refused')):
            records = notifier.run_local_pass()
        self.assertEqual(records, [])
        self.assertEqual(len(self.sent), 1)          # aucune résolution
        self.assertEqual(len(notifier.state['active']), 1)

    def test_a_healthy_console_resolves_and_notifies(self):
        notifier = self.notifier()
        admin, connect = self.queued()
        with admin, connect:
            notifier.run_local_pass()
        with mock.patch.object(pgbouncer_local, 'admin_dsn', return_value='admin'), \
                mock.patch('psycopg.connect',
                           return_value=self.console(waiting=0, maxwait=0)):
            records = notifier.run_local_pass()
        self.assertEqual([r['event'] for r in records], ['resolved'])
        self.assertEqual(len(self.sent), 2)
        self.assertIn('resolved', self.sent[1][1])
        self.assertEqual(notifier.state['active'], {})


class AlertWiring(unittest.TestCase):
    def test_type_is_ordered_pushed_and_labelled(self):
        self.assertIn('pgbouncer_pool_waiting', exploitation.ALERT_TYPES)
        self.assertIn('pgbouncer_pool_waiting', notify.DEFAULT_TYPES)
        self.assertIn('pgbouncer_pool_waiting', notify.URGENT_TYPES)
        self.assertIn('pgbouncer_pool_waiting', notify.LOCAL_TYPES)
        self.assertIn('pgbouncer_pool_waiting', notify.TYPE_LABELS)


if __name__ == '__main__':
    unittest.main()
