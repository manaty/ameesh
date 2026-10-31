# SPDX-License-Identifier: AGPL-3.0-only
"""Dedicated restore target: producer/consumer protocol, without root effects."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy/mise-a-jour' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


producer = load('restore_capacity', 'restauration-release.py')
recovery = load('restore_consumer', 'sauvegarde-release.py')


class CapacityTest(unittest.TestCase):
    def setUp(self):
        self.enrollment = dict(version='16', backup='/etc/ameesh-release/backup.json',
                               system_identifier='1234567890123456789')
        self.dsn = 'host=/run/ameesh-release-restore dbname=postgres user=restore password=CANARY require_auth=scram-sha-256'
        self.backup = dict(restore_dsn=self.dsn, restore_capacity='/root/capacity.json')
        self.observed = dict(system_identifier=self.enrollment['system_identifier'],
            data_directory=str(producer.layout('16')[0]), port='5432',
            listen_addresses='', external_tablespaces=0)
        self.parser = lambda dsn: dict(part.split('=', 1) for part in dsn.split())
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def value(self, free_bytes=10**12):
        with mock.patch.object(producer, 'protected', side_effect=Path):
            return producer.capacity(self.enrollment, self.backup, self.observed,
                parser=self.parser, free_bytes=free_bytes, now=2000000)

    def consumer(self, value, *, clock=2000000):
        result = recovery.RecoverySet.__new__(recovery.RecoverySet)
        result.config = self.backup
        result.clock = lambda: clock
        with mock.patch.object(recovery, 'protected_document', return_value=value):
            return result._capacity()

    def test_fresh_document_consumed_then_stale(self):
        value = self.value()
        self.assertEqual(set(value), recovery.CAPACITY_FIELDS)
        self.assertEqual(value['target_sha256'], hashlib.sha256(self.dsn.encode()).hexdigest())
        self.assertEqual(self.consumer(value), 10**12)
        self.assertEqual(self.consumer(value, clock=2000300), 10**12)
        self.assertIsNone(self.consumer(value, clock=2000301))
        self.assertNotIn('CANARY', json.dumps(value))

    def test_another_target_hash_is_refused(self):
        value = self.value()
        value['target_sha256'] = 'a' * 64
        self.assertIsNone(self.consumer(value))

    def test_insufficient_real_volume_refuses_before_copy(self):
        value = self.value(free_bytes=recovery.MINIMUM_FREE_BYTES + 199)
        instance = recovery.RecoverySet.__new__(recovery.RecoverySet)
        instance.config, instance.clock = self.backup, lambda: 2000000
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(recovery, 'protected_document', return_value=value), \
                mock.patch.object(recovery.backup, 'verify_restore') as restore:
            self.assertFalse(instance._restore(Path(tmp) / 'dump', Path(tmp) / 'proof',
                dict(source_bytes=100, artifact_sha256='a' * 64)))
        restore.assert_not_called()

    def test_live_cluster_or_redirected_target_refused(self):
        for dsn in (self.dsn.replace('/run/ameesh-release-restore', '127.0.0.1') + ' port=55432',
                    self.dsn.replace('/run/ameesh-release-restore', '127.0.0.1'),
                    'host=live port=55432 dbname=postgres user=restore',
                    self.dsn + ' hostaddr=10.0.0.1', self.dsn + ' service=live',
                    self.dsn + ' options=-cdata_directory=/live',
                    self.dsn.replace(' require_auth=scram-sha-256', ''),
                    self.dsn.replace('scram-sha-256', 'password'),
                    self.dsn + ' port=55432', self.dsn + ' port=5432'):
            with self.subTest(dsn=dsn):
                self.backup['restore_dsn'] = dsn
                with self.assertRaises(producer.RestoreError):
                    self.value()

    def test_libpq_routing_from_environment_refused(self):
        for key in ('PGSERVICE', 'PGSERVICEFILE', 'PGHOSTADDR', 'PGOPTIONS'):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: 'live'}):
                with self.assertRaises(producer.RestoreError):
                    self.value()

    def test_consumer_refuses_inherited_libpq_routing_before_restore(self):
        # #5170 N1: the producer's environment guard does not cover the
        # consumer, whose libpq and pg_restore inherit its own environment.
        instance = recovery.RecoverySet.__new__(recovery.RecoverySet)
        instance.config, instance.clock = self.backup, lambda: 2000000
        value = self.value()
        for key in recovery.LIBPQ_ROUTING_ENVIRONMENT:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {key: 'live'}), \
                    mock.patch.object(recovery, 'protected_document', return_value=value), \
                    mock.patch.object(recovery.backup, 'verify_restore') as restore:
                with self.assertRaises(recovery.RecoveryError):
                    instance._restore(Path(tmp) / 'dump', Path(tmp) / 'proof',
                                      dict(source_bytes=100, artifact_sha256='a' * 64))
                restore.assert_not_called()

    def test_capacity_output_never_targets_enrollment_or_configuration(self):
        # #5170 N2: refused before any write, not a replaced protected document.
        conf = producer.layout('16')[1]
        remote = dict(host='objects', upload_credentials='/srv/keys/upload.json',
                      restore_credentials='/srv/keys/restore.json')
        for output in (str(producer.ENROLLMENT), str(producer.INTENT),
                       self.enrollment['backup'], '/srv/probe.json',
                       '/etc/ameesh-release/other.json', str(conf / 'postgresql.conf'),
                       str(producer.layout('16')[0] / 'capacity.json'),
                       str(producer.INSTALL / 'capacity.json'),
                       str(producer.UNIT_ROOT / 'capacity.json'),
                       remote['upload_credentials'], remote['restore_credentials']):
            with self.subTest(output=output), \
                    mock.patch.object(producer, 'protected', side_effect=Path), \
                    mock.patch.object(producer, 'atomic') as write:
                backup = dict(self.backup, restore_capacity=output, remote=remote)
                with self.assertRaises(producer.RestoreError):
                    producer.capacity_output(backup, self.enrollment, '/srv/probe.json')
                write.assert_not_called()
        with mock.patch.object(producer, 'protected', side_effect=Path):
            self.assertEqual(producer.capacity_output(
                dict(self.backup, restore_capacity='/var/lib/ameesh-release-restore/capacity.json'),
                self.enrollment), Path('/var/lib/ameesh-release-restore/capacity.json'))

    def test_foreign_cluster_volume_or_tablespace_refused(self):
        for key, invalid in (('system_identifier', '9876543210987654321'),
                             ('data_directory', '/var/lib/postgresql/live'),
                             ('port', '55432'), ('listen_addresses', '*'),
                             ('external_tablespaces', 1)):
            with self.subTest(key=key), mock.patch.dict(self.observed, {key: invalid}):
                with self.assertRaises(producer.RestoreError):
                    self.value()

    def test_probe_uses_actual_volume_then_atomically_publishes(self):
        with mock.patch.object(producer, 'read_document', side_effect=[self.enrollment, self.backup]), \
                mock.patch.object(producer, 'inspect_cluster', return_value=self.observed), \
                mock.patch.object(producer, 'protected', side_effect=Path), \
                mock.patch.object(producer.os, 'statvfs') as volume, \
                mock.patch.object(producer, 'atomic') as write:
            volume.return_value.f_bavail = 123
            volume.return_value.f_frsize = 4096
            try:
                import psycopg.conninfo
            except ImportError:
                self.skipTest('psycopg parser unavailable')
            value = producer.probe()
        volume.assert_called_once_with(producer.layout('16')[0])
        self.assertEqual(value['free_bytes'], 123 * 4096)
        self.assertEqual(json.loads(write.call_args.args[1]), value)

    def test_root_timer_and_isolated_cluster_service(self):
        units = producer.units('16', '/opt/release/bin/python3')
        cluster = units[producer.NAME + '.service']
        probe = units[producer.NAME + '-capacity.service']
        timer = units[producer.NAME + '-capacity.timer']
        self.assertIn('User=ameesh-restore', cluster)
        self.assertIn('RuntimeDirectoryMode=0700', cluster)
        self.assertIn('config_file=/etc/ameesh-release-restore/postgresql.conf', cluster)
        self.assertIn('User=root', probe)
        self.assertIn(' -I ', probe)
        self.assertIn('OnUnitActiveSec=60s', timer)
        self.assertIn('TimeoutStartSec=120', probe)

    def test_atomic_output_private_and_replaces_exactly(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(producer, 'protected', side_effect=Path):
            path = Path(tmp) / 'capacity.json'
            path.write_text('previous')
            producer.atomic(path, b'{"fresh":true}')
            self.assertEqual(path.read_bytes(), b'{"fresh":true}')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(Path(tmp).iterdir()), [path])
            path.unlink()
            path.symlink_to(Path(tmp) / 'other')
            with self.assertRaises(producer.RestoreError):
                producer.atomic(path, b'forged')
            self.assertFalse((Path(tmp) / 'other').exists())

    def test_invalid_interpreter_and_version_cannot_inject_unit(self):
        with self.assertRaises(producer.RestoreError):
            producer.units('16', '/bin/python\nExecStart=/bin/false')
        with self.assertRaises(producer.RestoreError):
            producer.units('16\nExecStart=evil', '/bin/python3')


class InstallationTest(unittest.TestCase):
    def test_repeat_install_and_symmetric_remove_touch_only_disposable_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, conf = root / 'data-parent/data', root / 'etc/cluster'
            (root / 'units').mkdir()
            (root / 'etc').mkdir()
            output = root / 'capacity.json'
            backup = root / 'etc/backup.json'
            backup.write_text(json.dumps(dict(restore_capacity=str(output))))
            calls = []
            def effect(args):
                calls.append(args)
                if any(str(item).endswith('/bin/initdb') for item in args):
                    (data / 'PG_VERSION').write_text('16')
                return ''
            real_is_file = Path.is_file
            def available(path):
                if str(path) == '/usr/lib/postgresql/16/bin/initdb':
                    return True
                return real_is_file(path)
            account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(),
                                      pw_dir='/nonexistent', pw_shell='/usr/sbin/nologin')
            with mock.patch.multiple(producer, ENROLLMENT=root / 'etc/probe.json',
                        INTENT=root / 'etc/intent.json', INSTALL=root / 'installed',
                        UNIT_ROOT=root / 'units'), \
                    mock.patch.object(producer, 'layout', return_value=(data, conf)), \
                    mock.patch.object(producer, 'protected', side_effect=Path), \
                    mock.patch.object(producer, 'read_document',
                        side_effect=lambda path: json.loads(Path(path).read_text())), \
                    mock.patch.object(producer, 'system_identifier', return_value='1234567890123456789'), \
                    mock.patch.object(producer, 'inspect_cluster',
                        return_value=dict(system_identifier='1234567890123456789')), \
                    mock.patch.object(producer, 'command', side_effect=effect), \
                    mock.patch.object(producer.pwd, 'getpwnam', return_value=account), \
                    mock.patch.object(producer.os, 'chown'), \
                    mock.patch.object(Path, 'is_file', available):
                producer.install('16', backup, '/opt/release/bin/python3', 'restore')
                producer.install('16', backup, '/opt/release/bin/python3', 'restore')
                self.assertEqual((conf / 'pg_hba.conf').read_text(),
                    'local all ameesh-restore peer\n'
                    'local all release_restore_admin peer map=release_admin\n'
                    'local all restore scram-sha-256\n')
                self.assertEqual((conf / 'pg_ident.conf').read_text(),
                    'release_admin root release_restore_admin\n')
                self.assertIn("listen_addresses = ''\n", (conf / 'postgresql.conf').read_text())
                initializations = [args for args in calls if any(
                    str(item).endswith('/bin/initdb') for item in args)]
                self.assertEqual(len(initializations), 1)
                output.write_text('fresh attestation')
                producer.remove()
                producer.remove()
                self.assertFalse(output.exists())
                self.assertFalse(data.exists())
                self.assertFalse(conf.exists())
                self.assertFalse(producer.INTENT.exists())
                self.assertFalse(producer.ENROLLMENT.exists())
                self.assertEqual(list(producer.UNIT_ROOT.iterdir()), [])
                self.assertTrue(backup.exists())
                stop = next(i for i, args in enumerate(calls)
                            if args[1:3] == ['stop', producer.NAME + '-capacity.service'])
                cluster_stop = next(i for i, args in enumerate(calls)
                            if args[1:] == ['disable', '--now', producer.NAME + '.service'])
                self.assertLess(stop, cluster_stop)

    def test_hba_role_cannot_add_rules_or_assume_admin(self):
        for role in ('all', 'replication', 'pg_monitor', producer.ADMIN,
                     producer.ACCOUNT, 'restore\nlocal all all trust', '"restore"'):
            with self.subTest(role=role), self.assertRaises(producer.RestoreError):
                producer.authentication(role)

    def test_existing_unenrolled_tree_is_never_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, conf = root / 'data', root / 'conf'
            data.mkdir()
            with mock.patch.object(producer, 'layout', return_value=(data, conf)), \
                    mock.patch.object(producer, 'protected', side_effect=Path), \
                    mock.patch.object(producer, 'INTENT', root / 'intent'), \
                    mock.patch.object(producer, 'ENROLLMENT', root / 'enrollment.json'), \
                    mock.patch.object(Path, 'is_file', return_value=True), \
                    mock.patch.object(producer, 'command') as command:
                with self.assertRaisesRegex(producer.RestoreError, 'unowned'):
                    producer.install('16', root / 'backup', '/bin/python3', 'restore')
            command.assert_not_called()
