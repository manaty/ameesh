# SPDX-License-Identifier: AGPL-3.0-only
"""VM operator SSH policy: syntax and effective restrictions."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'deploy/vm-adapter/remote/sshd-match.example'


class SshPolicyTest(unittest.TestCase):
    def test_restrictions_are_in_the_match_but_user_environment_is_global(self):
        text = TEMPLATE.read_text()
        self.assertNotIn('PermitUserEnvironment', text)
        for directive in ('AllowAgentForwarding no', 'AllowStreamLocalForwarding no',
                          'AllowTcpForwarding no', 'PermitTunnel no', 'PermitListen none'):
            self.assertIn('    ' + directive + '\n', text)

    def test_rendered_configuration_parses_and_disables_forwarding(self):
        sshd = shutil.which('sshd')
        keygen = shutil.which('ssh-keygen')
        if not sshd or not keygen:
            self.skipTest('qualification sshd reportée : sshd/ssh-keygen absent')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            key = Path(directory) / 'hostkey'
            subprocess.run([keygen, '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                           check=True, capture_output=True, timeout=5)
            config = Path(directory) / 'sshd_config'
            config.write_text('HostKey %s\nPermitUserEnvironment no\n' % key
                              + TEMPLATE.read_text())
            parsed = subprocess.run([sshd, '-t', '-f', str(config)],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(parsed.returncode, 0, parsed.stderr)
            effective = subprocess.run([
                sshd, '-T', '-f', str(config), '-C',
                'user=vm-operator,host=localhost,addr=127.0.0.1'],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(effective.returncode, 0, effective.stderr)
            lines = effective.stdout.splitlines()
            for expected in ('permituserenvironment no', 'allowagentforwarding no',
                             'allowstreamlocalforwarding no', 'allowtcpforwarding no',
                             'permittunnel no', 'permitlisten none'):
                self.assertIn(expected, lines)


import importlib.util
import json
import time
from types import SimpleNamespace
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    'vm_health', ROOT / 'deploy/vm-adapter/remote/checks/health.py')
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.boot = self.root / 'boot'
        self.boot.write_text('current-boot\n')
        self.policy = dict(receipt=str(self.root / 'health.json'), producer_uid=1000,
                           period_s=60, version='1.2.3', sha='a' * 40, mode='execute')
        self.receipt = dict(schema=health.SCHEMA, boot_id='current-boot', timestamp=1000,
                            version='1.2.3', sha='a' * 40, mode='execute',
                            pids=[123], version_mode=True, doctor=True)

    def reader(self, path, uid, mode):
        if str(path) == self.policy['receipt']:
            return self.receipt
        return {'health': self.policy}

    def check(self, kind='version-mode'):
        return health.check(kind, boot=self.boot, now=lambda: 1001, reader=self.reader)

    def test_fresh_green_receipt(self):
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.check('doctor'), 0)

    def test_stale_future_wrong_boot_version_sha_mode_and_bad_types_refuse(self):
        for key, value in (('timestamp', 800), ('timestamp', 1002),
                           ('timestamp', float('nan')), ('timestamp', True),
                           ('boot_id', 'previous-boot'), ('version', 'old'),
                           ('sha', 'b' * 40), ('mode', 'dry-run'), ('pids', []),
                           ('pids', [True]), ('version_mode', False),
                           ('schema', 'other')):
            with self.subTest(key=key, value=value), mock.patch.dict(self.receipt, {key: value}):
                self.assertEqual(self.check(), 1)
                self.assertEqual(self.check('doctor'), 1)

    def test_doctor_negative_is_never_green(self):
        self.receipt['doctor'] = False
        self.assertEqual(self.check('doctor'), 1)

    def test_absent_unreadable_malformed_and_timeout_refuse_at_entrypoint(self):
        for error in (FileNotFoundError(), PermissionError(), ValueError(),
                      subprocess.TimeoutExpired('check', 5)):
            with self.subTest(error=type(error)), mock.patch.object(
                    health, 'check', side_effect=error):
                self.assertEqual(health.main(['doctor']), 1)
                self.assertEqual(health.main(['version-mode']), 1)

    def test_document_rejects_symlink_wrong_owner_and_permissions(self):
        path = self.root / 'document'
        path.write_text('{}')
        path.chmod(0o640)
        self.assertEqual(health.document(path, os.geteuid(), 0o640), {})
        with self.assertRaises(ValueError):
            health.document(path, os.geteuid() + 1, 0o640)
        path.chmod(0o660)
        with self.assertRaises(ValueError):
            health.document(path, os.geteuid(), 0o640)
        link = self.root / 'link'
        link.symlink_to(path)
        with self.assertRaises(OSError):
            health.document(link, os.geteuid(), 0o640)
        path.write_text('{bad')
        path.chmod(0o640)
        with self.assertRaises(ValueError):
            health.document(path, os.geteuid(), 0o640)

    def test_atomic_replacement_and_group_readable_permissions(self):
        path = self.root / 'published'
        health.atomic_receipt(path, self.receipt)
        self.assertEqual(json.loads(path.read_text()), self.receipt)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.receipt['doctor'] = False
        health.atomic_receipt(path, self.receipt)
        self.assertFalse(json.loads(path.read_text())['doctor'])
        self.assertEqual(list(self.root.glob('.health-*')), [])

    def test_producer_uses_real_verifier_and_live_mode_not_declared_mode(self):
        proc = self.root / 'proc' / '123'
        proc.mkdir(parents=True)
        proc.joinpath('cmdline').write_bytes(b'/venv/bin/python\0/venv/bin/ameesh\0run\0--dry-run\0')
        verifier = SimpleNamespace(verifier=mock.Mock())
        run = mock.Mock(return_value='MainPID=123\nActiveState=active\nSubState=running\nNeedDaemonReload=no\n')
        self.assertEqual(health.runner_state(
            'ameesh-runner-agent@probe.service', Path('/venv'), verifier,
            time.monotonic() + 5, proc=self.root / 'proc', run=run), (123, 'dry-run'))
        verifier.verifier.assert_called_once_with('123', Path('/venv'))
        verifier.verifier.side_effect = PermissionError()
        with self.assertRaises(PermissionError):
            health.runner_state('ameesh-runner-agent@probe.service', Path('/venv'), verifier,
                                time.monotonic() + 5, proc=self.root / 'proc', run=run)

    def test_producer_failure_replaces_previous_green_and_never_leaks_error(self):
        path = self.root / 'health.json'
        path.write_text(json.dumps(self.receipt))
        card = dict(venv='/venv', verifier='/missing-verifier', units=[
            'ameesh-runner-agent@probe.service'], environment_file='/private/env', receipt=str(path))
        with mock.patch.object(health.os, 'geteuid', return_value=1000):
            self.assertEqual(health.produce(boot=self.boot, reader=lambda *args: card), 1)
        result = json.loads(path.read_text())
        self.assertFalse(result['doctor'])
        self.assertFalse(result['version_mode'])
        self.assertNotIn('missing-verifier', path.read_text())

    def test_command_enforces_deadline(self):
        with self.assertRaises(TimeoutError):
            health.command(['/usr/bin/false'], time.monotonic() - 1)
        with mock.patch.object(health.subprocess, 'run', side_effect=subprocess.TimeoutExpired('probe', 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                health.command(['/usr/bin/false'], time.monotonic() + 1)

    def test_producer_green_runs_full_doctor_with_environment_file_and_rechecks_pids(self):
        path = self.root / 'health.json'
        verifier = self.root / 'verifier.py'
        verifier.write_text('from pathlib import Path\ndef venv_installe(venv): return Path("/live-venv")\n')
        unit = 'ameesh-runner-agent@probe.service'
        card = dict(venv='/venv', verifier=str(verifier), units=[unit],
                    environment_file='/private/env', receipt=str(path))
        calls = []

        def command(argv, end, **kwargs):
            calls.append(argv)
            if 'list-units' in argv:
                return json.dumps([dict(unit=unit, active='active')])
            if '-c' in argv:
                return json.dumps(dict(version='1.2.3', source={'url': 'file:///store/src-' + 'a' * 40}))
            return ''

        with mock.patch.object(health.os, 'geteuid', return_value=1000), \
                mock.patch.object(health, 'command', side_effect=command), \
                mock.patch.object(health, 'runner_state', return_value=(123, 'execute')) as state:
            self.assertEqual(health.produce(boot=self.boot, reader=lambda *args: card), 0)
        result = json.loads(path.read_text())
        self.assertTrue(result['doctor'])
        self.assertTrue(result['version_mode'])
        self.assertEqual((result['sha'], result['pids']), ('a' * 40, [123]))
        self.assertEqual(state.call_count, 2)
        doctor = calls[-1]
        self.assertIn('--property=EnvironmentFile=/private/env', doctor)
        self.assertEqual(doctor[-5:], ['/live-venv/bin/python', '-I', '-m', 'ameesh', 'doctor'])

    def test_dispatch_timeout_is_refusal_with_five_second_bound(self):
        spec = importlib.util.spec_from_file_location(
            'vm_remote_6296', ROOT / 'deploy/vm-adapter/remote/vm-remote.py')
        remote = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(remote)
        cfg = json.loads((ROOT / 'deploy/vm-adapter/remote/enrollment.example.json').read_text())
        cfg['checks']['doctor'] = ['/usr/bin/stat', '/etc/os-release']
        runner = mock.Mock(side_effect=subprocess.TimeoutExpired('check', 5))
        with mock.patch.object(remote, 'private_json', return_value=cfg), \
                mock.patch.object(remote.os, 'geteuid', return_value=1001):
            self.assertEqual(remote.main(['dispatch'], runner=runner, environ={
                'SSH_ORIGINAL_COMMAND': remote.PREFIX + 'doctor'}), 1)
        self.assertEqual(runner.call_args.kwargs['timeout'], 5)
        self.assertEqual(runner.call_args.kwargs['env'], remote.ENV)
        self.assertEqual(runner.call_args.kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(runner.call_args.kwargs['stderr'], subprocess.DEVNULL)
