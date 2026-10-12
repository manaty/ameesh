# SPDX-License-Identifier: AGPL-3.0-only
"""Agent-host deployment through fake SSH: no VM, unit or production database."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest

from tests import support  # isolate the workstation environment

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/mise-a-jour/vm.sh'

SSH = r'''#!/usr/bin/env python3
import os, subprocess, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
command = sys.argv[-1]
with (state / 'ssh.log').open('a') as f:
    f.write(command + '\n')
command = command.replace('/home/ameesh/.local/share/ameesh/venv/bin/ameesh', os.environ['FAKE_AMEESH'])
command = command.replace('/home/ameesh/.local/share/ameesh/venv/bin/python', os.environ['FAKE_DB_PYTHON'])
command = command.replace('$HOME/.config/ameesh/env', str(state / 'runner.env'))
env = dict(os.environ, FAKE_REMOTE='1')
sys.exit(subprocess.call(['bash', '-c', command], env=env))
'''
RUNUSER = r'''#!/usr/bin/env python3
import os, sys
assert sys.argv[1:4] == ['-u', 'ameesh', '--'], sys.argv
os.execvp(sys.argv[4], sys.argv[4:])
'''
AMEESH = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
assert os.environ.get('FAKE_REMOTE') == '1', 'registry must be read on the host'
assert os.environ.get('HOME') == '/home/ameesh'
assert os.environ.get('XDG_RUNTIME_DIR') == '/run/user/1001'
assert os.environ.get('DBUS_SESSION_BUS_ADDRESS') == 'unix:path=/run/user/1001/bus'
assert os.environ.get('AMEESH_DSN') == json.loads((state / 'expected-env.json').read_text())['AMEESH_DSN']
assert os.environ.get('AMEESH_SCHEMA') == 'test_runner_schema'
scenario = (state / 'scenario').read_text()
if sys.argv[1:] == ['list', '--json']:
    if scenario == 'list-error': sys.exit(1)
    if scenario == 'bad-json': print('not json'); sys.exit(0)
    print((state / 'agents.json').read_text())
elif sys.argv[1] == 'show':
    name = sys.argv[2]
    row = next(a for a in json.loads((state / 'agents.json').read_text()) if a['name'] == name)
    row = dict(row, lease_owner='runner-' + name + '@host-agents', lease_epoch=7, lease_expires_ts=time.time()+300)
    if scenario == 'expired': row['lease_expires_ts'] = time.time()-1
    if scenario == 'no-lease': row['lease_owner'] = None
    if scenario == 'foreign-owner': row['lease_owner'] = 'another-runner'
    if scenario == 'default-owner': row['lease_owner'] = 'host-agents:123'
    if scenario == 'db-ahead': row['lease_expires_ts'] = time.time()+30
    if scenario == 'moved-before': row['host'] = 'elsewhere'
    if scenario == 'always-busy': row['status'] = 'running'
    if (state / ('restart-' + name)).exists():
        row['lease_epoch'] = 9 if scenario != 'stale-lease' else 7
        if scenario == 'moved': row['host'] = 'elsewhere'
    if scenario == 'busy-once' and name == 'dev9' and not (state / 'busy-seen').exists():
        (state / 'busy-seen').touch(); row['status'] = 'running'
    print(json.dumps(row))
elif sys.argv[1] == 'doctor':
    print('doctor ok')
else: sys.exit(2)
'''
SYSTEMCTL = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
assert args[0] == '--user', args
unit = args[-1]
with (state / 'units.log').open('a') as f: f.write(' '.join(args) + '\n')
scenario = (state / 'scenario').read_text()
if 'restart' in args:
    if scenario == 'restart-error': sys.exit(1)
    (state / ('restart-' + unit.split('@')[1].removesuffix('.service'))).touch()
elif 'show' in args:
    if 'MainPID' in args: print('123')
    elif scenario == 'bad-execstart': print('unreadable')
    elif scenario == 'execstart-error': sys.exit(1)
    else:
        owner = 'runner-' + unit.split('@')[1].removesuffix('.service') + '@host-agents'
        option = '--runner-id ' + owner
        if scenario == 'equals-owner': option = '--runner-id=' + owner
        if scenario == 'abbreviated-owner': option = '--r=' + owner
        if scenario == 'last-owner': option = '--runner-id wrong --runner-id=' + owner
        if scenario == 'default-owner': option = ''
        print('{ path=/bin/runner ; argv[]=/bin/runner run ' + option + ' ; }')
elif 'is-active' in args:
    if scenario == 'inactive': sys.exit(3)
    print('active')
'''

SYSTEMD_RUN = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
assert args[:7] == ['--user', '--pipe', '--wait', '--quiet', '-p',
                    'EnvironmentFile=%h/.config/ameesh/env', '--'], args
with (state / 'systemd-run.log').open('a') as f: f.write(' '.join(args) + '\n')
# The manager supplies these values; this stub deliberately does not source
# or reimplement EnvironmentFile. A separate test exercises the real parser.
env = dict(os.environ, **json.loads((state / 'expected-env.json').read_text()))
os.execvpe(args[7], args[7:], env)
'''

DB_PYTHON = r'''#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
assert os.environ.get('FAKE_REMOTE') == '1'
assert os.environ.get('AMEESH_DSN') == __import__('json').loads((Path(os.environ['FAKE_STATE']) / 'expected-env.json').read_text())['AMEESH_DSN']
assert os.environ.get('AMEESH_SCHEMA') == 'test_runner_schema'
assert sys.argv[1] == '-c' and 'db.connect(config.load())' in sys.argv[2]
assert 'clock_timestamp()' in sys.argv[2]
scenario = (Path(os.environ['FAKE_STATE']) / 'scenario').read_text()
print(time.time() + (60 if scenario == 'db-ahead' else 0))
'''


class AgentHostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-503-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'; self.state.mkdir()
        (self.state / 'runner.env').write_text('AMEESH_DSN=test-runner-dsn\nAMEESH_SCHEMA=test_runner_schema\n')
        (self.state / 'expected-env.json').write_text(json.dumps(dict(AMEESH_DSN='test-runner-dsn', AMEESH_SCHEMA='test_runner_schema')))
        self.bin = self.root / 'bin'; self.bin.mkdir()
        (self.bin / 'python3').symlink_to(sys.executable)
        self.home = self.root / 'home'; self.home.mkdir()
        self.config = self.root / 'bascule.env'
        self.config.write_text('AGENTS_SSH=root@vm.invalid\nHOTE_AGENTS=host-agents\nPERSONAS_VM=obsolete\n')
        for name, source in [('ssh', SSH), ('runuser', RUNUSER), ('ameesh', AMEESH),
                             ('systemctl', SYSTEMCTL), ('systemd-run', SYSTEMD_RUN), ('db-python', DB_PYTHON), ('sleep', '#!/bin/sh\nexit 0\n'),
                             ('id', '#!/bin/sh\necho 1001\n'),
                             ('journalctl', '#!/bin/sh\nexit 0\n')]:
            path = self.bin / name; path.write_text(source); path.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), BASCULE_ENV=str(self.config),
                        FAKE_STATE=str(self.state), FAKE_AMEESH=str(self.bin / 'ameesh'),
                        FAKE_DB_PYTHON=str(self.bin / 'db-python'),
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'])
        self.agents = [dict(name=n, host=h, mode=m, status=s) for n,h,m,s in [
            ('dev9', 'host-agents', 'execute', 'idle'),
            ('dev8', 'host-agents', 'execute', 'idle'),
            ('elsewhere', 'other-host', 'execute', 'idle'),
            ('observer', 'host-agents', 'observe', 'idle'),
            ('stopped', 'host-agents', 'execute', 'stopped')]]

    def run_vm(self, action='redemarrer', scenario='lease', agents=None):
        (self.state / 'scenario').write_text(scenario)
        (self.state / 'agents.json').write_text(json.dumps(self.agents if agents is None else agents))
        return subprocess.run(['bash', str(SCRIPT), 'agents', action], env=self.env,
                              capture_output=True, text=True, timeout=120)

    def units(self):
        path = self.state / 'units.log'
        return path.read_text() if path.exists() else ''

    def test_missing_runner_env_fails_before_any_execution(self):
        (self.state / 'runner.env').unlink()
        p = self.run_vm()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('runner environment file unreadable', p.stderr)
        self.assertEqual(self.units(), '')
        self.assertFalse((self.state / 'systemd-run.log').exists())

    def test_systemd_supplies_literal_environment(self):
        expected = 'postgresql://test@invalid/mesh_$USER"quoted"#fragment'
        (self.state / 'runner.env').write_text(
            'AMEESH_DSN=postgresql://test@invalid/mesh_$USER"quoted"#fragment\n'
            'AMEESH_SCHEMA=test_runner_schema\n')
        (self.state / 'expected-env.json').write_text(json.dumps(dict(
            AMEESH_DSN=expected, AMEESH_SCHEMA='test_runner_schema')))
        p = self.run_vm('controler')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('EnvironmentFile=%h/.config/ameesh/env',
                      (self.state / 'systemd-run.log').read_text())

    def test_restart_uses_remote_registry_and_user_units(self):
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        restarts = [l for l in self.units().splitlines() if 'restart' in l]
        self.assertEqual(restarts, ['--user restart ameesh-runner-agent@dev8.service',
                                    '--user restart ameesh-runner-agent@dev9.service'])
        self.assertIn('dev9 : redémarré', p.stdout)

    def test_restart_waits_for_agent_out_of_turn(self):
        p = self.run_vm(scenario='busy-once')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((self.state / 'busy-seen').exists())

    def test_restart_rejects_unchanged_lease(self):
        for scenario in ['stale-lease']:
            with self.subTest(scenario=scenario):
                p = self.run_vm(scenario=scenario)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn('bail', p.stderr)
                self.assertIn('journalctl --user', p.stderr)
                self.assertNotIn('redémarré', p.stdout)

    def test_restart_without_prior_lease_reports_warning(self):
        p = self.run_vm(scenario='no-lease')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('WARNING dev8: not checked', p.stdout)
        self.assertIn('WARNING: 2 persona(s) not checked', p.stdout)
        self.assertIn('restart ameesh-runner-agent@dev8.service', self.units())

    def test_runner_id_forms_and_default_are_accepted(self):
        for scenario in ['equals-owner', 'abbreviated-owner', 'last-owner', 'default-owner']:
            with self.subTest(scenario=scenario):
                p = self.run_vm('controler', scenario)
                self.assertEqual(p.returncode, 0, p.stderr)

    def test_unreadable_execstart_prevents_restart(self):
        for scenario in ['bad-execstart', 'execstart-error']:
            with self.subTest(scenario=scenario):
                p = self.run_vm(scenario=scenario)
                self.assertNotEqual(p.returncode, 0)
                self.assertNotIn('restart ', self.units())

    def test_check_rejects_foreign_owner(self):
        p = self.run_vm('controler', 'foreign-owner')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bail', p.stderr)

    def test_check_uses_database_clock(self):
        p = self.run_vm('controler', 'db-ahead')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bail', p.stderr)

    def test_check_detects_active_unit_without_lease(self):
        p = self.run_vm('controler', 'no-lease')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bail', p.stderr)
        self.assertNotIn('restart', self.units())

    def test_check_rejects_expired_lease(self):
        p = self.run_vm('controler', 'expired')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bail', p.stderr)

    def test_check_accepts_held_lease_and_is_read_only(self):
        p = self.run_vm('controler')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('contrôles verts', p.stdout)
        self.assertNotIn('restart', self.units())

    def test_unit_failures_do_not_report_success(self):
        for scenario in ['restart-error', 'inactive']:
            with self.subTest(scenario=scenario):
                p = self.run_vm(scenario=scenario)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn('ameesh-runner-agent@dev8.service', p.stderr)
                self.assertNotIn('redémarré', p.stdout)

    def test_moved_agent_is_skipped_before_restart(self):
        p = self.run_vm(scenario='moved-before')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.units(), '')
        self.assertIn('placement modifié', p.stdout)

    def test_host_change_after_restart_is_not_a_valid_proof(self):
        p = self.run_vm(scenario='moved')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bail', p.stderr)

    def test_long_turn_times_out_without_restart(self):
        self.env['ATTENTE_MAX'] = '0'
        p = self.run_vm(scenario='always-busy')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('toujours en tour', p.stderr)
        self.assertEqual(self.units(), '')

    def test_registry_errors_fail_before_restart(self):
        for scenario in ['list-error', 'bad-json']:
            with self.subTest(scenario=scenario):
                p = self.run_vm(scenario=scenario)
                self.assertNotEqual(p.returncode, 0)
                self.assertEqual(self.units(), '')

    def test_empty_host_is_not_green(self):
        p = self.run_vm(agents=[])
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.units(), '')
        self.assertIn('aucun agent', p.stderr)

    def test_registry_name_is_validated_before_shell_interpolation(self):
        p = self.run_vm(agents=[dict(name='dev9;touch injected', host='host-agents',
                                     mode='execute', status='idle')])
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.units(), '')
        self.assertIn('invalide', p.stderr)

    def test_all_valid_registry_names_restart(self):
        names = ['dev_1', 'Dev1', 'dev.1', 'd' * 64]
        p = self.run_vm(agents=[dict(name=n, host='host-agents', mode='execute', status='idle')
                                for n in names])
        self.assertEqual(p.returncode, 0, p.stderr)
        for name in names:
            self.assertIn('restart ameesh-runner-agent@' + name + '.service', self.units())

    def test_invalid_name_is_named_and_skipped_without_blocking_valid_agents(self):
        bad = dict(name='bad;touch injected', host='host-agents', mode='execute', status='idle')
        p = self.run_vm(agents=self.agents + [bad])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("agent 'bad;touch injected' : nom invalide", p.stderr)
        self.assertIn('restart ameesh-runner-agent@dev9.service', self.units())
        self.assertNotIn('injected', self.units())

    def test_mode_refuses_unsupported_action(self):
        p = self.run_vm('installer')
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse((self.state / 'ssh.log').exists())


class SystemdEnvironmentFileTest(unittest.TestCase):
    def test_real_manager_keeps_literal_values(self):
        manager = shutil.which('systemd-run')
        ctl = shutil.which('systemctl')
        if not manager or not ctl:
            self.skipTest('systemd user manager unavailable')
        available = subprocess.run([ctl, '--user', 'show-environment'],
                                   capture_output=True, timeout=10)
        if available.returncode:
            self.skipTest('systemd user manager unavailable')
        with tempfile.TemporaryDirectory(prefix='ameesh-503-systemd-') as tmp:
            path = Path(tmp) / 'runner.env'
            expected = 'postgresql://test@invalid/mesh_$USER"quoted"#fragment'
            path.write_text('AMEESH_DSN=' + expected + '\n')
            result = subprocess.run([
                manager, '--user', '--pipe', '--wait', '--quiet', '-p',
                'EnvironmentFile=' + str(path), '--', sys.executable, '-c',
                'import os; print(os.environ["AMEESH_DSN"])'],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)


class SharedLeasePredicateTest(unittest.TestCase):
    def test_shared_criterion_for_restart_and_check(self):
        import time
        row = dict(status='idle', host='host-agents', lease_owner='runner',
                   lease_epoch=9, lease_expires_ts=time.time()+300)
        cases = [
            (row, 7, 'restart', 0),
            (dict(row, lease_epoch=7), 7, 'restart', 1),
            (dict(row, lease_owner=None), 7, 'restart', 1),
            (dict(row, status='stopped', lease_owner=None), 7, 'restart', 0),
            (dict(row, lease_owner='foreign'), 7, 'restart', 1),
            (row, 0, 'check', 0),
            (dict(row, lease_expires_ts=0), 0, 'check', 1),
            (dict(row, lease_expires_ts=None), 0, 'check', 1),
            (dict(row, status='stopped', lease_owner=None), 0, 'check', 0),
        ]
        for value, before, phase, expected in cases:
            with self.subTest(row=value, phase=phase):
                p = subprocess.run(['bash', '-c',
                    '. "$1"; bail_valide "$2" runner "$3"',
                    'test', str(ROOT / 'deploy/mise-a-jour/bail.sh'), str(before), str(time.time())],
                    input=json.dumps(value), capture_output=True, text=True)
                self.assertEqual(p.returncode, expected, p.stderr)


if __name__ == '__main__':
    unittest.main()
