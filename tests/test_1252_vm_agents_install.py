# SPDX-License-Identifier: AGPL-3.0-only
"""#1252 — `vm.sh agents installer|retour`: install and roll back the frozen code
on an agent VM, in the account's own layout (~/.local/share/ameesh: clone,
snapshots src-<sha7>, previous-sha, venv).

The host layout is a temporary directory. `ssh`, `scp`, `runuser`, `systemctl`,
`systemd-run`, `ameesh` and the venv's `pip` are fake; `git` is real, on a real
clone, so checkout, snapshots and previous-sha are exercised for real. No live
host, unit or database is touched, and the fake SSH refuses to execute a remote
command that still contains a live `/home/ameesh` path.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/mise-a-jour/vm.sh'
BAIL = ROOT / 'deploy/mise-a-jour/bail.sh'

SSH = r'''#!/usr/bin/env python3
import os, subprocess, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
vmhome = Path(os.environ['FAKE_VM_HOME'])
vmtmp = Path(os.environ['FAKE_VM_TMP'])
live = '/home/ameesh'
sentinel = '@@VMHOME@@'
command = sys.argv[-1]
command = command.replace(live, sentinel)
# A live path left here would reach the host: refuse before any fake path,
# which legitimately contains /home/ameesh under this harness's TMPDIR.
if live in command:
    sys.exit('untranslated remote path: ' + command)
command = command.replace('/tmp/ameesh.bundle', str(vmtmp / 'ameesh.bundle'))
command = command.replace(sentinel, str(vmhome))
command = command.replace('$HOME/.config/ameesh/env', str(vmhome / '.config/ameesh/env'))
with (state / 'ssh.log').open('a') as f:
    f.write(command.replace('\n', ' ') + '\n')
sys.exit(subprocess.call(['bash', '-c', command], env=dict(os.environ, FAKE_REMOTE='1')))
'''

SCP = r'''#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
source = Path(sys.argv[-2])
dest = sys.argv[-1]
assert dest.startswith('root@vm.invalid:'), dest
target = Path(os.environ['FAKE_VM_TMP']) / Path(dest.split(':', 1)[1]).name
shutil.copy2(source, target)
with (state / 'scp.log').open('a') as f:
    f.write(' '.join(sys.argv[1:]) + '\n')
'''

RUNUSER = r'''#!/usr/bin/env python3
import os, sys
assert sys.argv[1:4] == ['-u', 'ameesh', '--'], sys.argv
os.execvp(sys.argv[4], sys.argv[4:])
'''

ID = '#!/bin/sh\necho 1001\n'

SYSTEMCTL = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
assert args[0] == '--user', args
unit = args[-1]
with (state / 'systemctl.log').open('a') as f:
    f.write(' '.join(args) + '\n')
if 'restart' in args:
    name = unit.split('@')[1].removesuffix('.service')
    (state / ('restart-' + name)).touch()
elif 'show' in args:
    if 'MainPID' in args:
        print('123')
    else:
        print('{ path=/bin/runner ; argv[]=/bin/runner run --runner-id '
              'runner-' + unit.split('@')[1].removesuffix('.service') + '@host-agents ; }')
elif 'is-active' in args:
    print('active')
'''

JOURNALCTL = '#!/bin/sh\nexit 0\n'
SLEEP = '#!/bin/sh\nexit 0\n'

SYSTEMD_RUN = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
assert args[:7] == ['--user', '--pipe', '--wait', '--quiet', '-p',
                    'EnvironmentFile=%h/.config/ameesh/env', '--'], args
with (state / 'systemd-run.log').open('a') as f:
    f.write(' '.join(args) + '\n')
env = dict(os.environ, **json.loads((state / 'expected-env.json').read_text()))
os.execvpe(args[7], args[7:], env)
'''

AMEESH = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
assert os.environ.get('FAKE_REMOTE') == '1', 'registry must be read on the host'
scenario = (state / 'scenario').read_text().strip()
agents = json.loads((state / 'agents.json').read_text())
if sys.argv[1:] == ['list', '--json']:
    if scenario == 'list-error':
        sys.exit(1)
    print(json.dumps(agents))
elif sys.argv[1] == 'show':
    name = sys.argv[2]
    row = dict(next(a for a in agents if a['name'] == name),
               lease_owner='runner-%s@host-agents' % name, lease_epoch=7,
               lease_expires_ts=time.time() + 3600)
    if (state / ('restart-' + name)).exists():
        row['lease_epoch'] = 9
    print(json.dumps(row))
elif sys.argv[1] == 'doctor':
    print('doctor ok')
else:
    sys.exit(2)
'''

DB_PYTHON = r'''#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
with (state / 'python.log').open('a') as f:
    f.write(' '.join(sys.argv[1:]).replace('\n', ' ') + '\n')
assert sys.argv[1] == '-c', sys.argv
code = sys.argv[2]
if 'schema_migrations' in code:
    print((state / 'versions.txt').read_text().strip())
elif 'clock_timestamp' in code:
    print(time.time())
elif 'import psycopg' in code:
    print('psycopg 3.2.0 (fake)')
else:
    sys.exit('unexpected database probe: ' + code)
'''

PIP = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
log = state / 'pip.log'
with log.open('a') as f:
    f.write(' '.join(sys.argv[1:]) + '\n')
fail_at = int((state / 'pip-fail-at').read_text()) if (state / 'pip-fail-at').exists() else 0
if fail_at and len(log.read_text().splitlines()) >= fail_at:
    sys.exit('pip failure (test)')
'''


class VmAgentsHarness(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-1252-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        (self.state / 'scenario').write_text('lease\n')
        (self.state / 'agents.json').write_text(json.dumps([
            dict(name='dev9', host='host-agents', mode='execute', status='idle')]))
        (self.state / 'expected-env.json').write_text(json.dumps(
            dict(AMEESH_DSN='postgresql://test@invalid/mesh', AMEESH_SCHEMA='test')))
        self.vm = self.root / 'vm'
        self.vmhome = self.vm / 'home/ameesh'
        self.vmtmp = self.vm / 'tmp'
        self.vmtmp.mkdir(parents=True)
        self.parts = self.vmhome / '.local/share/ameesh'
        self.venv = self.parts / 'venv'
        self.src = self.parts / 'src'
        for path in (self.vmhome / '.config/ameesh',
                     self.vmhome / '.local/state/ameesh/mise-a-jour',
                     self.venv / 'bin',
                     self.venv / 'lib/python3.11/site-packages/ameesh-1.9.0.dist-info'):
            path.mkdir(parents=True)
        (self.vmhome / '.config/ameesh/env').write_text(
            'AMEESH_DSN=postgresql://test@invalid/mesh\nAMEESH_SCHEMA=test\n')
        # The fake ssh refuses to execute an untranslated /home/ameesh path, so
        # a missing fake command would fail the test rather than reach the host.
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.bin / 'python3').symlink_to(sys.executable)
        for name, text in [('ssh', SSH), ('scp', SCP), ('runuser', RUNUSER), ('id', ID),
                           ('systemctl', SYSTEMCTL), ('journalctl', JOURNALCTL),
                           ('sleep', SLEEP), ('systemd-run', SYSTEMD_RUN),
                           ('ameesh', AMEESH), ('db-python', DB_PYTHON)]:
            path = self.bin / name
            path.write_text(text)
            path.chmod(0o755)
        (self.venv / 'bin/python').write_text(DB_PYTHON)
        (self.venv / 'bin/pip').write_text(PIP)
        (self.venv / 'bin/ameesh').write_text(AMEESH)
        (self.venv / 'bin/python').chmod(0o755)
        (self.venv / 'bin/pip').chmod(0o755)
        (self.venv / 'bin/ameesh').chmod(0o755)
        self.workhome = self.root / 'home'
        self.workhome.mkdir()
        config = self.root / 'bascule.env'
        config.write_text('AGENTS_SSH=root@vm.invalid\nHOTE_AGENTS=host-agents\n')
        self.env = dict(os.environ, HOME=str(self.workhome), BASCULE_ENV=str(config),
                        FAKE_STATE=str(self.state), FAKE_VM_HOME=str(self.vmhome),
                        FAKE_VM_TMP=str(self.vmtmp),
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'])
        self.old = self.git(ROOT, 'rev-parse', 'HEAD~1')
        self.target = self.git(ROOT, 'rev-parse', 'HEAD')
        self._clone_at_old()
        self._install_versions(self.target)
        self._write_direct_url(self.old)
        (self.parts / 'previous-sha').write_text(self.old + '\n')

    # -- helpers ------------------------------------------------------------
    def git(self, where, *args):
        return subprocess.run(['git', '-C', str(where), *args], check=True,
                              capture_output=True, text=True, timeout=120).stdout.strip()

    def head(self, where):
        return self.git(where, 'rev-parse', 'HEAD')

    def _clone_at_old(self, at=None):
        if self.src.exists():
            shutil.rmtree(self.src)
        subprocess.run(['git', 'clone', '-q', '--shared', '--no-checkout',
                        str(ROOT), str(self.src)], check=True, timeout=300)
        subprocess.run(['git', '-C', str(self.src), 'checkout', '-q', '--detach',
                        at or self.old], check=True, timeout=120)

    def _src_containing_only(self, sha):
        """A clone that has the object but no usable HEAD (no previous code)."""
        shutil.rmtree(self.src)
        subprocess.run(['git', 'init', '-q', str(self.src)], check=True)
        subprocess.run(['git', '-C', str(self.src), 'fetch', '-q', str(ROOT), sha],
                       check=True, timeout=120)

    def _write_direct_url(self, sha):
        info = self.venv / 'lib/python3.11/site-packages/ameesh-1.9.0.dist-info'
        (info / 'direct_url.json').write_text(json.dumps(
            {'url': (self.parts / ('src-' + sha[:7])).as_uri()}))

    def _install_versions(self, sha):
        names = self.git(ROOT, 'ls-tree', '--name-only', sha, 'src/ameesh/migrations/')
        versions = sorted({re.match(r'0*(\d+)_', n.split('/')[-1]).group(1)
                           for n in names.splitlines() if n.endswith('.sql')},
                          key=int)
        (self.state / 'versions.txt').write_text('\n'.join(versions) + '\n')

    def run_vm(self, action, *args):
        return subprocess.run(['bash', str(SCRIPT), 'agents', action, *args],
                              env=self.env, capture_output=True, text=True, timeout=300)

    def pip_calls(self):
        log = self.state / 'pip.log'
        return log.read_text().splitlines() if log.exists() else []


class VmAgentsInstallTest(VmAgentsHarness):
    """The commit is already in the VM's clone: checkout and install it."""

    # -- installation -------------------------------------------------------
    def test_install_checks_out_the_frozen_code_and_records_previous(self):
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.head(self.src), self.target)
        snapshot = self.parts / ('src-' + self.target[:7])
        self.assertEqual(self.head(snapshot), self.target)
        self.assertTrue((self.parts / ('src-' + self.old[:7])).is_dir())
        self.assertEqual((self.parts / 'previous-sha').read_text().strip(), self.old)
        calls = self.pip_calls()
        self.assertEqual(len(calls), 2, calls)
        for call in calls:
            self.assertIn('src-' + self.target[:7], call)
        backup = self.vmhome / '.local/state/ameesh/mise-a-jour' / ('env-' + self.target[:7])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertIn('installé depuis src-', p.stdout)
        self.assertIn('redemarrer', p.stdout)

    def test_install_does_not_restart_the_runners(self):
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse((self.state / 'systemctl.log').exists())

    def test_install_refuses_when_previous_code_is_untraceable(self):
        self._src_containing_only(self.target)
        (self.venv / 'lib/python3.11/site-packages/ameesh-1.9.0.dist-info'
         / 'direct_url.json').unlink()
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("code d'avant introuvable", p.stderr)
        self.assertEqual(self.pip_calls(), [])

    def test_install_refuses_when_migrations_are_missing_from_the_base(self):
        versions = (self.state / 'versions.txt').read_text().split()
        (self.state / 'versions.txt').write_text('\n'.join(versions[:-1]) + '\n')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('migrations', p.stderr)
        self.assertIn('poste.sh migrer', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertFalse((self.parts / ('src-' + self.target[:7])).exists())
        self.assertEqual(self.pip_calls(), [])

    def test_install_refuses_and_changes_nothing_when_the_env_is_unreadable(self):
        (self.vmhome / '.config/ameesh/env').unlink()
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.head(self.src), self.old)
        self.assertFalse((self.parts / ('src-' + self.target[:7])).exists())
        self.assertEqual(self.pip_calls(), [])

    def test_install_never_overwrites_an_existing_snapshot(self):
        old_snapshot = self.parts / ('src-' + self.old[:7])
        shutil.copytree(self.src, old_snapshot)
        (old_snapshot / 'MARKER').write_text('keep')
        target_snapshot = self.parts / ('src-' + self.target[:7])
        subprocess.run(['git', 'clone', '-q', '--shared', '--no-checkout',
                        str(ROOT), str(target_snapshot)], check=True, timeout=300)
        subprocess.run(['git', '-C', str(target_snapshot), 'checkout', '-q',
                        '--detach', self.target], check=True, timeout=120)
        (target_snapshot / 'MARKER').write_text('keep')
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((old_snapshot / 'MARKER').read_text(), 'keep')
        self.assertEqual((target_snapshot / 'MARKER').read_text(), 'keep')
        self.assertEqual(self.head(target_snapshot), self.target)

    def test_failed_install_keeps_previous_sha_so_retour_stays_possible(self):
        (self.parts / 'previous-sha').write_text(self.git(ROOT, 'rev-parse', 'HEAD~2') + '\n')
        (self.state / 'pip-fail-at').write_text('2\n')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual((self.parts / 'previous-sha').read_text().strip(),
                         self.git(ROOT, 'rev-parse', 'HEAD~2'))
        self.assertEqual(self.head(self.src), self.target)

    # -- rollback -----------------------------------------------------------
    def test_retour_refuses_without_previous_sha(self):
        (self.parts / 'previous-sha').unlink()
        p = self.run_vm('retour')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('aucun retour possible', p.stderr)
        self.assertEqual(self.head(self.src), self.old)

    def test_retour_refuses_an_invalid_previous_sha(self):
        (self.parts / 'previous-sha').write_text('pas-un-sha\n')
        p = self.run_vm('retour')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('previous-sha invalide', p.stderr)
        self.assertEqual(self.head(self.src), self.old)

    def test_retour_refuses_a_commit_unknown_to_both_sides(self):
        (self.parts / 'previous-sha').write_text('0' * 40 + '\n')
        p = self.run_vm('retour')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("retour impossible d'ici", p.stderr)
        self.assertEqual(self.head(self.src), self.old)

    def test_retour_reinstalls_the_previous_code_then_restarts_and_controls(self):
        self._clone_at_old(self.target)
        self._write_direct_url(self.target)
        (self.parts / 'previous-sha').write_text(self.old + '\n')
        old_snapshot = self.parts / ('src-' + self.old[:7])
        subprocess.run(['git', 'clone', '-q', '--shared', '--no-checkout',
                        str(ROOT), str(old_snapshot)], check=True, timeout=300)
        subprocess.run(['git', '-C', str(old_snapshot), 'checkout', '-q',
                        '--detach', self.old], check=True, timeout=120)
        p = self.run_vm('retour')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        for call in self.pip_calls():
            self.assertIn('src-' + self.old[:7], call)
        self.assertIn('restart ameesh-runner-agent@dev9.service',
                      (self.state / 'systemctl.log').read_text())
        self.assertIn('dev9 : redémarré', p.stdout)
        self.assertIn('contrôles verts', p.stdout)

    def test_control_reports_the_installed_code_without_restarting(self):
        p = self.run_vm('controler')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('code installé', p.stdout)
        self.assertIn(self.old[:7], p.stdout)
        self.assertNotIn('restart', (self.state / 'systemctl.log').read_text())


class VmAgentsBundleInstallTest(VmAgentsHarness):
    """A VM that cannot reach origin receives the commit as a git bundle."""

    def setUp(self):
        super().setUp()
        # The workstation repository becomes a throw-away one, so the bundle is
        # built from local history without touching the real repository.
        self.depot = self.root / 'depot'
        (self.depot / 'deploy/mise-a-jour').mkdir(parents=True)
        shutil.copy2(SCRIPT, self.depot / 'deploy/mise-a-jour/vm.sh')
        shutil.copy2(BAIL, self.depot / 'deploy/mise-a-jour/bail.sh')
        (self.depot / 'src/ameesh/migrations').mkdir(parents=True)
        (self.depot / 'src/ameesh/migrations/0001_init.sql').write_text('-- test\n')
        subprocess.run(['git', 'init', '-q', str(self.depot)], check=True)
        subprocess.run(['git', '-C', str(self.depot), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.depot), '-c', 'user.email=t@invalid',
                        '-c', 'user.name=test', 'commit', '-q', '-m', 'previous'],
                       check=True)
        self.old = self.git(self.depot, 'rev-parse', 'HEAD')
        (self.depot / 'src/ameesh/migrations/0002_next.sql').write_text('-- test\n')
        subprocess.run(['git', '-C', str(self.depot), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.depot), '-c', 'user.email=t@invalid',
                        '-c', 'user.name=test', 'commit', '-q', '-m', 'target'],
                       check=True)
        self.target = self.git(self.depot, 'rev-parse', 'HEAD')
        # The VM clone holds the old commit only, and has no origin at all.
        shutil.rmtree(self.src)
        subprocess.run(['git', 'init', '-q', str(self.src)], check=True)
        subprocess.run(['git', '-C', str(self.src), 'fetch', '-q', str(self.depot), self.old],
                       check=True, timeout=120)
        subprocess.run(['git', '-C', str(self.src), 'checkout', '-q', '--detach', self.old],
                       check=True)
        self._write_direct_url(self.old)
        (self.parts / 'previous-sha').write_text(self.old + '\n')
        (self.state / 'versions.txt').write_text('1\n2\n')

    def run_vm(self, action, *args):
        return subprocess.run(['bash', str(self.depot / 'deploy/mise-a-jour/vm.sh'),
                               'agents', action, *args], env=self.env,
                              capture_output=True, text=True, timeout=300)

    def test_install_sends_a_git_bundle_when_the_vm_lacks_the_commit(self):
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((self.state / 'scp.log').exists(), p.stdout)
        self.assertIn('ameesh.bundle', (self.state / 'scp.log').read_text())
        self.assertEqual(self.head(self.src), self.target)
        self.assertEqual(self.head(self.parts / ('src-' + self.target[:7])), self.target)
        self.assertEqual((self.parts / 'previous-sha').read_text().strip(), self.old)
        self.assertFalse((self.vmtmp / 'ameesh.bundle').exists())


if __name__ == '__main__':
    unittest.main()
