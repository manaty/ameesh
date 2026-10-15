# SPDX-License-Identifier: AGPL-3.0-only
"""Health evidence: real versioned release files, simulated systemd and CLI.

No root, SSH, systemd, PostgreSQL migration or production effect is executed:
the only child process is a fixture script in a disposable release directory.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    'release_health', Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour/sante-release.py')
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)


class Database:
    def __init__(self, dsn='postgresql://health:CANARY-HEALTH@localhost/fixture-db', rows=()):
        self.cfg = SimpleNamespace(dsn=dsn)
        self.rows = list(rows)

    def query(self, sql, params=()):
        if 'to_regclass' in sql:
            return [{'ok': True}]
        return [{'version': version, 'checksum': checksum} for version, checksum in self.rows]


class System:
    def __init__(self, states):
        self.states, self.shown = dict(states), []

    def show(self, unit, properties, seconds):
        self.shown.append((unit, dict(properties), seconds))
        return dict(self.states[unit])


class Turn:
    """The pinned authenticated turn companion, simulated here."""

    def __init__(self, result=True):
        self.result, self.calls = result, []

    def one_turn(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class HealthProbeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ameesh-release-health-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = self.base / 'store'
        self.store.mkdir(mode=0o700); self.store.chmod(0o711)
        self.card = dict(units=['ameesh-runner@one.service'],
                         properties={'ameesh-runner@one.service': {'MemoryMax': '2G'}},
                         guard='ameesh-guard.service', seconds=30)
        self.states = {
            'ameesh-runner@one.service': {'LoadState': 'loaded', 'NeedDaemonReload': 'no',
                                          'ActiveState': 'active', 'MemoryMax': '2G'},
            'ameesh-guard.service': {'LoadState': 'loaded', 'NeedDaemonReload': 'no',
                                     'ActiveState': 'active'}}
        self.system = System(self.states)
        self.turn = Turn()
        self.target_document = dict(commit='a' * 40, python='/fixture/python',
                                    python_sha256='f' * 64, wheels=[], configuration={})
        self.previous_document = dict(commit='b' * 40, python='/fixture/python',
                                      python_sha256='f' * 64, wheels=[], configuration={})
        self.target = self.install(self.target_document, migrations={'0001_init.sql': 'select 1;',
                                                                     '0002_more.sql': 'select 2;'})
        self.previous = self.install(self.previous_document, migrations={'0001_init.sql': 'select 1;'})
        (self.store / 'current').symlink_to(self.target)
        self.db = Database(rows=self.applied(self.target_document))

    def install(self, document, *, migrations, exit_code=0):
        identity = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        release = document['commit'] + '-' + identity
        root = self.store / release
        directory = root / 'source/src/ameesh/migrations'
        directory.mkdir(parents=True, mode=0o700)
        for name, sql in migrations.items():
            (directory / name).write_text(sql)
        (root / 'venv/bin').mkdir(parents=True, mode=0o700)
        prefix = self.base / ('probe-' + release[:12])
        executable = root / 'venv/bin/python'
        executable.write_text("#!/bin/sh\n"
                              "printf '%%s\\n' \"$@\" > '%s.args'\n"
                              "env > '%s.env'\n"
                              "stat -c '%%a' \"$AMEESH_CONFIG\" > '%s.mode'\n"
                              "cp \"$AMEESH_CONFIG\" '%s.config'\n"
                              "exit %d\n" % (prefix, prefix, prefix, prefix, exit_code))
        executable.chmod(0o755)
        (root / 'ready.json').write_text(json.dumps(document, sort_keys=True))
        # Same traversable modes as environnement-versionne's store: the
        # controller runs this release's own runner as the enrolled executor.
        for directory in (root, root / 'venv', root / 'venv/bin'):
            directory.chmod(0o711)
        return release

    def applied(self, document):
        rows = []
        directory = self.store / (document['commit'] + '-' + hashlib.sha256(
            json.dumps(document, sort_keys=True).encode()).hexdigest()) / 'source/src/ameesh/migrations'
        for path in sorted(directory.glob('*.sql')):
            rows.append((int(path.name.split('_', 1)[0]),
                         hashlib.sha256(path.read_text().encode()).hexdigest()))
        return rows

    def manifest(self, release):
        return dict(host='fixture-host', commit=release[:40], release=release, database='fixture-db',
                    units=['ameesh-runner@one.service'], policy='policy-' + 'c' * 64,
                    rollback_until=2_000_000_000)

    def request(self, step, release, operation='release-fixture'):
        manifest = self.manifest(release)
        return dict(operation=operation, manifest_digest=health.protocol.identity(manifest),
                    action_id=operation + ':' + step, manifest=manifest, step=step)

    def probe(self, document):
        prefix = self.base / ('probe-' + (document['commit'] + '-' + hashlib.sha256(
            json.dumps(document, sort_keys=True).encode()).hexdigest())[:12])
        files = {}
        for name in ('args', 'env', 'mode', 'config'):
            path = Path(str(prefix) + '.' + name)
            files[name] = path.read_text() if path.exists() else None
        return files

    def value(self, db=None):
        return health.HealthProbe(db or self.db, store=self.store, card=self.card,
                                  system=self.system, turn=self.turn)

    def perform(self, step, document=None, db=None, release=None):
        document = document or self.target_document
        release = release if release is not None else document
        identity = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        return self.value(db).perform(step, self.request(step, document['commit'] + '-' + identity),
                                      release)

    def test_health_accepts_the_activated_release_and_probes_with_a_private_config(self):
        self.assertTrue(self.perform('health'))
        self.assertEqual(self.system.shown,
                         [('ameesh-runner@one.service', {'MemoryMax': '2G'}, 30),
                          ('ameesh-guard.service', {}, 30)])
        probe = self.probe(self.target_document)
        self.assertEqual(json.loads(probe['config']), {'backend': 'postgres', 'dsn': self.db.cfg.dsn})
        self.assertEqual(probe['mode'].strip(), '600')
        self.assertNotIn('CANARY-HEALTH', probe['args'])
        self.assertNotIn('CANARY-HEALTH', probe['env'])
        self.assertIn('-I', probe['args'].split())
        self.assertIn('doctor', probe['args'].split())
        config_path = next(line.split('=', 1)[1] for line in probe['env'].splitlines()
                           if line.startswith('AMEESH_CONFIG='))
        self.assertFalse(Path(config_path).exists(), 'the private probe config must not survive')

    def test_health_refuses_missing_modified_or_unknown_schema_versions(self):
        self.assertFalse(self.perform('health', db=Database(rows=[])))
        self.assertFalse(self.perform('health', db=Database(rows=[(1, '0' * 64), self.applied(
            self.target_document)[1]])))
        unknown = self.applied(self.target_document) + [(99, 'e' * 64)]
        self.assertFalse(self.perform('health', db=Database(rows=unknown)))

    def test_recovery_health_probes_the_restored_prior_release_and_tolerates_leftovers(self):
        (self.store / '.operations').mkdir(mode=0o700)
        operations = self.store / '.operations/release-fixture.json'
        operations.write_text(json.dumps({'previous': self.previous, 'target': self.target}))
        operations.chmod(0o600)
        (self.store / 'current').unlink()
        (self.store / 'current').symlink_to(self.previous)
        rows = self.applied(self.previous_document) + [(99, 'e' * 64)]
        self.assertTrue(self.perform('recovery_health', db=Database(rows=rows)))
        self.assertIsNotNone(self.probe(self.previous_document)['args'])
        self.assertIsNone(self.probe(self.target_document)['args'])

    def test_health_refuses_pointer_drift_and_effective_state_failures(self):
        (self.store / 'current').unlink()
        (self.store / 'current').symlink_to(self.previous)
        self.assertFalse(self.perform('health'))
        (self.store / 'current').unlink()
        (self.store / 'current').symlink_to(self.target)
        for unit, change in (('ameesh-runner@one.service', {'LoadState': 'not-found'}),
                             ('ameesh-runner@one.service', {'NeedDaemonReload': 'yes'}),
                             ('ameesh-runner@one.service', {'ActiveState': 'inactive'}),
                             ('ameesh-runner@one.service', {'MemoryMax': '4G'}),
                             ('ameesh-guard.service', {'ActiveState': 'inactive'})):
            original = self.states[unit]
            self.system.states[unit] = dict(original, **change)
            self.assertFalse(self.perform('health'), change)
            self.system.states[unit] = original

    def test_probe_failure_is_definitive_and_a_timeout_is_ambiguous(self):
        failed_document = dict(self.target_document, commit='d' * 40)
        failed = self.install(failed_document, migrations={'0001_init.sql': 'select 1;'}, exit_code=1)
        (self.store / 'current').unlink()
        (self.store / 'current').symlink_to(failed)
        self.assertFalse(self.perform('health', document=failed_document,
                                      db=Database(rows=self.applied(failed_document))))
        (self.store / 'current').unlink()
        (self.store / 'current').symlink_to(self.target)
        with mock.patch.object(health.subprocess, 'run',
                               side_effect=health.subprocess.TimeoutExpired('probe', 30)):
            with self.assertRaises(health.HealthError):
                self.perform('health')
        with mock.patch.object(health.subprocess, 'run', side_effect=OSError('unavailable')):
            with self.assertRaises(health.HealthError):
                self.perform('health')

    def test_release_receipt_and_release_document_are_rederived(self):
        document = json.loads((self.store / self.target / 'ready.json').read_text())
        document['python_sha256'] = '0' * 64
        (self.store / self.target / 'ready.json').write_text(json.dumps(document, sort_keys=True))
        self.assertFalse(self.perform('health'))
        self.assertFalse(self.perform('health', release=dict(self.target_document, commit='9' * 40)))

    def test_invalid_health_request_is_refused(self):
        request = self.request('health', self.target)
        with self.assertRaises(health.HealthError):
            self.value().perform('backup', request, self.target_document)
        broken = dict(request, action_id='release-fixture:recovery_health')
        with self.assertRaises(health.HealthError):
            self.value().perform('health', broken, self.target_document)
        with self.assertRaises(health.HealthError):
            self.value().perform('health', dict(request, manifest_digest='0' * 64), self.target_document)

    def test_health_runs_one_authenticated_turn_after_the_local_probe(self):
        self.assertTrue(self.perform('health'))
        self.assertEqual(len(self.turn.calls), 1)
        call = self.turn.calls[0]
        self.assertEqual(call['operation'], 'release-fixture')
        self.assertEqual(call['step'], 'health')
        self.assertEqual(call['release'], self.target)
        self.assertEqual(Path(call['root']), self.store / self.target)
        self.assertIsNotNone(self.probe(self.target_document)['args'])

    def test_a_refused_turn_or_a_missing_backend_never_completes_health(self):
        self.turn.result = False
        self.assertFalse(self.perform('health'))
        bare = health.HealthProbe(self.db, store=self.store, card=self.card, system=self.system)
        with self.assertRaisesRegex(health.HealthError, 'authenticated turn'):
            bare.perform('health', self.request('health', self.target), self.target_document)

    def test_an_ambiguous_turn_raises_instead_of_claiming_health(self):
        self.turn.result = health.HealthError('controlled turn unavailable')
        with self.assertRaises(health.HealthError):
            self.perform('health')


if __name__ == '__main__':
    unittest.main()
