# SPDX-License-Identifier: AGPL-3.0-only
"""Root symlink traversal and bounded VM health failure diagnostics."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import support  # isolate the session environment

ROOT = Path(__file__).resolve().parents[1] / 'deploy/vm-adapter/remote'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


remote = load('remote_6327', ROOT / 'vm-remote.py')
health = load('health_6327', ROOT / 'checks/health.py')


class RootExecutableTest(unittest.TestCase):
    def check(self, *, links=None, overrides=None, path='/usr/bin/python3'):
        links = links or {'/usr/bin/python3': 'python3.12'}
        overrides = overrides or {}
        visited = []

        def info(item):
            name = str(item)
            visited.append(name)
            mode = (stat.S_IFLNK | 0o777 if name in links else
                    stat.S_IFREG | 0o755 if name.endswith('python3.12') else
                    stat.S_IFDIR | 0o755)
            uid, mode = overrides.get(name, (0, mode))
            return SimpleNamespace(st_uid=uid, st_mode=mode)

        with mock.patch.object(remote.Path, 'lstat', info), \
                mock.patch.object(remote.os, 'readlink', side_effect=lambda p: links[str(p)]):
            remote.root_executable(path)
        return visited

    def test_root_link_mode_0777_is_accepted(self):
        self.assertIn('/usr/bin/python3.12', self.check())

    def test_alternatives_chain_and_each_parent_are_checked(self):
        visited = self.check(links={'/usr/bin/python3': '/etc/alternatives/python3',
                                   '/etc/alternatives/python3': '../../usr/bin/python3.12'})
        for path in ('/etc', '/etc/alternatives', '/etc/alternatives/python3',
                     '/usr', '/usr/bin', '/usr/bin/python3.12'):
            self.assertIn(path, visited)

    def test_non_root_link_target_or_parent_and_writable_target_or_parent_refuse(self):
        links = {'/usr/bin/python3': '/etc/alternatives/python3',
                 '/etc/alternatives/python3': '/opt/runtime/python3.12'}
        for path, kind in (('/usr/bin/python3', stat.S_IFLNK),
                           ('/etc/alternatives/python3', stat.S_IFLNK),
                           ('/opt/runtime/python3.12', stat.S_IFREG),
                           ('/etc/alternatives', stat.S_IFDIR),
                           ('/opt/runtime', stat.S_IFDIR)):
            for uid, bits in ((1000, 0o755), (0, 0o775), (0, 0o757)):
                if uid == 0 and kind == stat.S_IFLNK:
                    continue
                with self.subTest(path=path, uid=uid, bits=bits), self.assertRaises(remote.Refus):
                    self.check(links=links, overrides={path: (uid, kind | bits)})

    def test_symlinked_parent_is_checked_before_its_target(self):
        with self.assertRaises(remote.Refus):
            self.check(links={'/usr': '/opt/usr'}, overrides={
                '/opt': (1000, stat.S_IFDIR | 0o755)})

    def test_dispatch_checks_enrolled_executable_before_running_fixed_argv(self):
        cfg = json.loads((ROOT / 'enrollment.example.json').read_text())
        for verb in ('doctor', 'version-mode'):
            runner = mock.Mock(return_value=SimpleNamespace(returncode=0))
            with self.subTest(verb=verb), \
                    mock.patch.object(remote, 'private_json', return_value=cfg), \
                    mock.patch.object(remote.os, 'geteuid', return_value=1001), \
                    mock.patch.object(remote, 'root_executable') as validate:
                code = remote.main(['dispatch'], runner=runner, environ={
                    'SSH_ORIGINAL_COMMAND': remote.PREFIX + verb})
                self.assertEqual(code, 0)
                validate.assert_called_once_with('/usr/bin/python3')
                self.assertEqual(runner.call_args.args[0], cfg['checks'][verb])
                validate.side_effect = remote.Refus('unsafe')
                runner.reset_mock()
                self.assertEqual(remote.main(['dispatch'], runner=runner, environ={
                    'SSH_ORIGINAL_COMMAND': remote.PREFIX + verb}), 1)
                runner.assert_not_called()

    def test_loop_and_non_regular_target_refuse(self):
        with self.assertRaises(remote.Refus):
            self.check(links={'/usr/bin/python3': 'python3'})
        with self.assertRaises(remote.Refus):
            self.check(links={'/usr/bin/python3': '/etc'})


class DiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.boot = self.root / 'boot'
        self.boot.write_text('boot\n')
        self.receipt = self.root / 'receipt'
        self.unit = 'ameesh-runner-agent@probe.service'
        self.card = dict(venv='/venv', verifier='/verifier.py', units=[self.unit],
                         environment_file='/secret-environment', receipt=str(self.receipt))
        self.secret = 'postgresql://private-password@secret-host/db'

    def run_producer(self, stage=None):
        verifier = SimpleNamespace(venv_installe=mock.Mock(return_value=Path('/live')))
        loader = SimpleNamespace(exec_module=mock.Mock())
        spec = SimpleNamespace(loader=loader)
        state_calls = 0

        def fail():
            raise ValueError(self.secret)

        def command(argv, end, **kw):
            current = 'inventory' if 'list-units' in argv else 'metadata' if '-c' in argv else 'doctor'
            if stage == current:
                fail()
            if current == 'inventory':
                return json.dumps([dict(unit=self.unit, active='active')])
            return json.dumps(dict(version='1.2.3', source={'url': 'file:///src-' + 'a' * 40}))

        def state(*args):
            nonlocal state_calls
            state_calls += 1
            if stage == ('runners-before' if state_calls == 1 else 'runners-after'):
                fail()
            return 123, 'execute'

        if stage == 'verifier':
            loader.exec_module.side_effect = ValueError(self.secret)
        if stage == 'venv':
            verifier.venv_installe.side_effect = ValueError(self.secret)
        reader = mock.Mock(return_value=self.card)
        if stage == 'setup':
            reader.side_effect = ValueError(self.secret)
        stderr = io.StringIO()
        with mock.patch.object(health.os, 'geteuid', return_value=1000), \
                mock.patch.object(health.importlib.util, 'spec_from_file_location', return_value=spec), \
                mock.patch.object(health.importlib.util, 'module_from_spec', return_value=verifier), \
                mock.patch.object(health, 'runner_state', side_effect=state), \
                mock.patch.object(health, 'command', side_effect=command), \
                contextlib.redirect_stderr(stderr):
            code = health.produce(boot=self.boot, reader=reader)
        return code, stderr.getvalue()

    def test_each_failed_probe_step_records_only_stage_and_exception_class(self):
        for stage in ('verifier', 'inventory', 'runners-before', 'venv', 'metadata',
                      'doctor', 'runners-after'):
            with self.subTest(stage=stage):
                code, output = self.run_producer(stage)
                receipt = json.loads(self.receipt.read_text())
                self.assertEqual(code, 1)
                self.assertEqual(receipt['reason'], stage + ':ValueError')
                self.assertEqual(output, 'vm-health ' + receipt['reason'] + '\n')
                self.assertFalse(receipt['doctor'])
                self.assertFalse(receipt['version_mode'])
                for secret in (self.secret, self.card['environment_file']):
                    self.assertNotIn(secret, output + self.receipt.read_text())

    def test_boot_failure_has_red_receipt_and_reason(self):
        self.boot.unlink()
        code, output = self.run_producer()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(self.receipt.read_text())['reason'], 'boot:FileNotFoundError')
        self.assertEqual(output, 'vm-health boot:FileNotFoundError\n')

    def test_setup_failure_is_logged_without_trusting_a_receipt_path(self):
        code, output = self.run_producer('setup')
        self.assertEqual((code, output), (1, 'vm-health setup:ValueError\n'))
        self.assertFalse(self.receipt.exists())

    def test_green_receipt_schema_and_silence_unchanged(self):
        code, output = self.run_producer()
        self.assertEqual((code, output), (0, ''))
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(set(receipt), {'schema', 'boot_id', 'timestamp', 'version', 'sha',
                                      'mode', 'venv', 'pids', 'version_mode', 'doctor'})
        self.assertTrue(receipt['doctor'])

    def test_publish_and_reader_errors_and_custom_exception_names_do_not_leak(self):
        with mock.patch.object(health, 'atomic_receipt', side_effect=OSError(self.secret)):
            self.assertEqual(self.run_producer(), (1, 'vm-health publish:OSError\n'))
        secret_error = type('SECRET_CLASS_NAME', (Exception,), {})
        for verb in ('doctor', 'version-mode'):
            output = io.StringIO()
            with mock.patch.object(health, 'check', side_effect=secret_error(self.secret)), \
                    contextlib.redirect_stderr(output):
                self.assertEqual(health.main([verb]), 1)
            self.assertEqual(output.getvalue(), 'vm-health read:Exception\n')

    def test_service_keeps_stderr_in_journal(self):
        service = (ROOT / 'checks/ameesh-vm-health.service').read_text()
        self.assertIn('StandardOutput=null\n', service)
        self.assertIn('StandardError=journal\n', service)
