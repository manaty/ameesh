# SPDX-License-Identifier: AGPL-3.0-only
"""#1252 — `vm.sh agents installer|retour`: install and roll back the frozen code
on an agent VM, in the account's own layout (~/.local/share/ameesh: clone,
snapshots src-<sha7>, previous-sha, recovery journal, venv).

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
import socket
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
import os, re, subprocess, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
vmhome = Path(os.environ['FAKE_VM_HOME'])
vmtmp = Path(os.environ['FAKE_VM_TMP'])
live = '/home/ameesh'
sentinel = '@@VMHOME@@'
command = sys.argv[-1]
# Un chemin déjà traduit (rendu par un `mktemp` distant, relu par le script)
# ne doit pas l'être deux fois : on le met de côté avant le reste.
command = command.replace(str(vmtmp), '@@VMTMP@@')
command = command.replace(live, sentinel)
# A live path left here would reach the host: refuse before any fake path,
# which legitimately contains /home/ameesh under this harness's TMPDIR.
if live in command:
    sys.exit('untranslated remote path: ' + command)
command = re.sub(r'/tmp/ameesh\.bundle(\.[A-Za-z0-9]+)?',
                 lambda m: '@@VMTMP@@/ameesh.bundle' + (m.group(1) or ''),
                 command)
command = command.replace(sentinel, str(vmhome))
command = command.replace('@@VMTMP@@', str(vmtmp))
command = command.replace('$HOME/.config/ameesh/env', str(vmhome / '.config/ameesh/env'))
with (state / 'ssh.log').open('a') as f:
    f.write(command.replace('\n', ' ') + '\n')
# Le poste doit voir le chemin de la VM (/tmp), pas le TMPDIR physique du
# harnais : celui-ci peut contenir des caractères refusés côté distant (~).
result = subprocess.run(['bash', '-c', command],
                        env=dict(os.environ, FAKE_REMOTE='1'),
                        stdout=subprocess.PIPE, text=True)
sys.stdout.write(result.stdout.replace(str(vmtmp), '/tmp'))
sys.exit(result.returncode)
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
# Le root distant crée le fichier sous son umask : sans le chmod 644 du script,
# le compte ameesh ne pourrait pas lire le paquet (#1252 R2).
os.chmod(target, 0o600)
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
name = unit.split('@')[1].removesuffix('.service')
with (state / 'systemctl.log').open('a') as f:
    f.write(' '.join(args) + '\n')
def stopped():
    return (state / ('stopped-' + name)).exists()
def reload_epoch():
    # un runner redémarré reprend son bail à une époque supérieure
    epoch = state / ('lease-epoch-' + name)
    epoch.write_text(str(int(epoch.read_text()) + 2 if epoch.exists() else 9))
if 'stop' in args:
    # #1152 : l'arrêt prouvé précède tout nouveau démarrage.
    (state / ('stopped-' + name)).touch()
elif 'start' in args:
    (state / ('stopped-' + name)).unlink(missing_ok=True)
    (state / ('restart-' + name)).touch()
    reload_epoch()
elif 'restart' in args:
    # Ancien chemin gardé visible pour les assertions qui l'interdisent.
    (state / ('restart-' + name)).touch()
    reload_epoch()
elif 'show' in args:
    if 'ActiveState' in args:
        print('inactive' if stopped() else 'active')
    elif 'MainPID' in args:
        print('0' if stopped() else '123')
    else:
        print('{ path=/bin/runner ; argv[]=/bin/runner run --runner-id '
              'runner-' + name + '@host-agents ; }')
elif 'is-active' in args:
    if stopped():
        sys.exit(3)
    print('active')
'''

JOURNALCTL = '#!/bin/sh\nexit 0\n'
SLEEP = '#!/bin/sh\nexit 0\n'

SYSTEMD_RUN = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
# #1133 : chemin absolu, systemd-run n'étend pas %h
assert args[:7] == ['--user', '--pipe', '--wait', '--quiet', '-p',
                    'EnvironmentFile=%s/.config/ameesh/env' % os.environ['HOME'], '--'], args
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
    epoch = state / ('lease-epoch-' + name)
    if epoch.exists():
        row['lease_epoch'] = int(epoch.read_text())
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
scenario = (state / 'scenario').read_text().strip()
if 'schema_migrations' in code:
    print((state / 'versions.txt').read_text().strip())
elif 'clock_timestamp' in code:
    print(time.time())
elif 'config.load().host' in code:
    if scenario == 'identity-error':
        sys.exit('installed package is broken (test)')
    print(os.environ.get('AMEESH_HOST') or '')
elif 'import psycopg' in code:
    print('psycopg 3.2.0 (fake)')
else:
    sys.exit('unexpected database probe: ' + code)
'''

PIP = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
log = state / 'pip.log'
with log.open('a') as f:
    f.write(' '.join(sys.argv[1:]) + '\n')
fail_at = int((state / 'pip-fail-at').read_text()) if (state / 'pip-fail-at').exists() else 0
if fail_at and len(log.read_text().splitlines()) >= fail_at:
    sys.exit('pip failure (test)')
# Un pip réussi publie direct_url.json sur la source installée : le venv pointe
# alors l'instantané même si l'installation s'arrête au pip suivant.
info = (Path(os.environ['FAKE_VM_HOME'])
        / '.local/share/ameesh/venv/lib/python3.11/site-packages/ameesh-1.9.0.dist-info')
if info.is_dir():
    (info / 'direct_url.json').write_text(json.dumps(
        {'url': Path(sys.argv[-1]).resolve().as_uri()}))
'''


class VmAgentsHarness(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        # Exerce aussi la séparation des chemins locaux/distants sur tout hôte.
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-1252-~')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        (self.state / 'scenario').write_text('lease\n')
        (self.state / 'agents.json').write_text(json.dumps([
            dict(name='dev9', host='host-agents', mode='execute', status='idle')]))
        (self.state / 'expected-env.json').write_text(json.dumps(
            dict(AMEESH_DSN='postgresql://test@invalid/mesh', AMEESH_SCHEMA='test',
                 AMEESH_HOST='host-agents')))
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
        # L'hôte de test ne doit pas fournir d'identité par accident : elle
        # vient de l'EnvironmentFile simulé ou du fichier de configuration.
        for nom in ('AMEESH_HOST', 'AGENT_MESH_HOST', 'AMEESH_CONFIG', 'AGENT_MESH_CONFIG'):
            self.env.pop(nom, None)
        # Le dépôt d'origine est bâti par le test : la CI clone la tête en
        # profondeur 1, l'historique du checkout n'est donc pas une dépendance
        # admissible (défaut de test, canon §3). Trois commits réels :
        # plus_ancien, old, target.
        self.origin = self.root / 'origin'
        self._build_origin()
        self.plus_ancien = self.git(self.origin, 'rev-parse', 'HEAD~2')
        self.old = self.git(self.origin, 'rev-parse', 'HEAD~1')
        self.target = self.git(self.origin, 'rev-parse', 'HEAD')
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

    def _build_origin(self):
        """Un dépôt d'origine à trois commits, indépendant de l'historique du
        checkout : la CI ne fournit que la tête (clone en profondeur 1)."""
        (self.origin / 'deploy/mise-a-jour').mkdir(parents=True)
        shutil.copy2(SCRIPT, self.origin / 'deploy/mise-a-jour/vm.sh')
        shutil.copy2(BAIL, self.origin / 'deploy/mise-a-jour/bail.sh')
        # #1155 x #1252 : vm.sh délègue la création du paquet à ce script
        # (union composée) — le banc le copie à côté, comme bail.sh.
        shutil.copy2(ROOT / 'deploy/mise-a-jour/paquet-git.sh',
                     self.origin / 'deploy/mise-a-jour/paquet-git.sh')
        shutil.copy2(ROOT / '.gitignore', self.origin / '.gitignore')
        (self.origin / 'src/ameesh/migrations').mkdir(parents=True)
        (self.origin / 'src/ameesh/__init__.py').write_text('# origine\n')
        subprocess.run(['git', 'init', '-q', str(self.origin)], check=True)
        for nom in ('0001_init.sql', '0002_next.sql', '0003_target.sql'):
            (self.origin / 'src/ameesh/migrations' / nom).write_text('-- test\n')
            subprocess.run(['git', '-C', str(self.origin), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(self.origin), '-c', 'user.email=t@invalid',
                            '-c', 'user.name=test', 'commit', '-q', '-m', nom], check=True)

    def _clone_at_old(self, at=None):
        if self.src.exists():
            shutil.rmtree(self.src)
        subprocess.run(['git', 'clone', '-q', '--shared', '--no-checkout',
                        str(self.origin), str(self.src)], check=True, timeout=300)
        subprocess.run(['git', '-C', str(self.src), 'checkout', '-q', '--detach',
                        at or self.old], check=True, timeout=120)

    def _src_containing_only(self, sha):
        """A clone that has the object but no usable HEAD (no previous code)."""
        shutil.rmtree(self.src)
        subprocess.run(['git', 'init', '-q', str(self.src)], check=True)
        subprocess.run(['git', '-C', str(self.src), 'fetch', '-q', str(self.origin), sha],
                       check=True, timeout=120)

    def _write_direct_url(self, sha):
        info = self.venv / 'lib/python3.11/site-packages/ameesh-1.9.0.dist-info'
        (info / 'direct_url.json').write_text(json.dumps(
            {'url': (self.parts / ('src-' + sha[:7])).as_uri()}))

    def _install_versions(self, sha):
        names = self.git(self.origin, 'ls-tree', '--name-only', sha,
                         'src/ameesh/migrations/')
        versions = sorted({re.match(r'0*(\d+)_', n.split('/')[-1]).group(1)
                           for n in names.splitlines() if n.endswith('.sql')},
                          key=int)
        (self.state / 'versions.txt').write_text('\n'.join(versions) + '\n')

    def run_vm(self, action, *args):
        # Le script est celui du poste de travail : son dépôt (DEPOT, déduit de
        # son chemin) est le dépôt d'origine du harnais, donc les SHA visés y
        # sont résolubles sans dépendre du checkout.
        return subprocess.run(['bash', str(self.origin / 'deploy/mise-a-jour/vm.sh'),
                               'agents', action, *args],
                              env=self.env, capture_output=True, text=True, timeout=300)

    def pip_calls(self):
        log = self.state / 'pip.log'
        return log.read_text().splitlines() if log.exists() else []

    def installed_url(self):
        info = (self.venv / 'lib/python3.11/site-packages'
                / 'ameesh-1.9.0.dist-info/direct_url.json')
        return json.loads(info.read_text())['url']

    def runner_env(self, **env):
        """EnvironmentFile simulé ; sans AMEESH_HOST, l'identité vient du JSON."""
        (self.state / 'expected-env.json').write_text(json.dumps(
            dict(AMEESH_DSN='postgresql://test@invalid/mesh', AMEESH_SCHEMA='test', **env)))

    def point_bascule_at(self, host):
        (self.root / 'bascule.env').write_text(
            'AGENTS_SSH=root@vm.invalid\nHOTE_AGENTS=%s\n' % host)

    def machine_host(self):
        return socket.gethostname().split('.')[0]

    def mask_tracked_source(self, snapshot, flag):
        """Modifie une source suivie en la masquant de `git status` (index)."""
        self.git(snapshot, 'update-index', flag, 'src/ameesh/__init__.py')
        (snapshot / 'src/ameesh/__init__.py').write_text('# modifié sous %s\n' % flag)
        self.assertEqual(self.git(snapshot, 'status', '--porcelain', '-uall',
                                  '--ignored'), '')


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
        self.assertFalse((self.parts / 'installation-en-cours').exists())
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

    def test_install_refuses_when_the_runner_identity_is_unreadable(self):
        (self.vmhome / '.config/ameesh/env').unlink()
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('identité', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertFalse((self.parts / ('src-' + self.target[:7])).exists())
        self.assertEqual(self.pip_calls(), [])

    def test_install_refuses_when_the_runner_host_is_not_hote_agents(self):
        (self.state / 'expected-env.json').write_text(json.dumps(
            dict(AMEESH_DSN='postgresql://test@invalid/mesh', AMEESH_SCHEMA='test',
                 AMEESH_HOST='wrong-host')))
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('wrong-host', p.stderr)
        self.assertIn('HOTE_AGENTS', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertFalse((self.parts / ('src-' + self.target[:7])).exists())
        self.assertFalse((self.parts / 'installation-en-cours').exists())
        self.assertEqual(self.pip_calls(), [])
        self.assertNotIn('fetch', (self.state / 'ssh.log').read_text())
        self.assertNotIn('checkout', (self.state / 'ssh.log').read_text())

    def test_install_refuses_when_bascule_env_names_another_host(self):
        # Même cas vu du fichier de paramètres : la VM dit host-agents.
        (self.root / 'bascule.env').write_text(
            'AGENTS_SSH=root@vm.invalid\nHOTE_AGENTS=wrong-host\n')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('host-agents', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertFalse((self.parts / ('src-' + self.target[:7])).exists())
        self.assertEqual(self.pip_calls(), [])

    def test_install_from_a_dirty_clone_still_snapshots_the_commit(self):
        # Un clone sale ne doit pas entrer dans l'instantané du code quitté.
        (self.src / 'src/ameesh/__init__.py').write_text('# modifié localement\n')
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        old_snapshot = self.parts / ('src-' + self.old[:7])
        self.assertEqual(self.head(old_snapshot), self.old)
        self.assertEqual(self.git(old_snapshot, 'status', '--porcelain', '-uall',
                                  '--ignored'), '')

    def test_identity_is_read_from_the_runner_environment_file(self):
        # Le paquet installé ne s'importe plus (installation inachevée) : le
        # repli lit le même EnvironmentFile et l'identité reste contrôlée.
        (self.state / 'scenario').write_text('identity-error\n')
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.head(self.src), self.target)

    # -- identité de repli : même résolution que config.load().host ----------
    def test_identity_fallback_reads_the_host_json_not_the_machine_name(self):
        # Paquet cassé, HOTE_AGENTS = nom machine, JSON host divergent : le
        # repli doit rendre le host du JSON et refuser, pas accepter le nom
        # machine (#1252 B3).
        (self.state / 'scenario').write_text('identity-error\n')
        machine = self.machine_host()
        self.point_bascule_at(machine)
        self.runner_env()
        (self.vmhome / '.config/ameesh/config.json').write_text(
            json.dumps({'host': 'wrong-configured-host'}))
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('wrong-configured-host', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertFalse((self.parts / ('src-' + self.target[:7])).exists())
        self.assertEqual(self.pip_calls(), [])

    def test_identity_fallback_accepts_the_host_json_of_hote_agents(self):
        (self.state / 'scenario').write_text('identity-error\n')
        machine = self.machine_host()
        self.point_bascule_at(machine)
        self.runner_env()
        (self.vmhome / '.config/ameesh/config.json').write_text(
            json.dumps({'host': machine}))
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.head(self.src), self.target)

    def test_identity_fallback_uses_the_machine_name_without_config(self):
        (self.state / 'scenario').write_text('identity-error\n')
        self.point_bascule_at(self.machine_host())
        self.runner_env()
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.head(self.src), self.target)

    def test_identity_fallback_uses_the_machine_name_when_json_has_no_host(self):
        (self.state / 'scenario').write_text('identity-error\n')
        self.point_bascule_at(self.machine_host())
        self.runner_env()
        (self.vmhome / '.config/ameesh/config.json').write_text(
            json.dumps({'schema': 'test'}))
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_identity_fallback_refuses_a_null_host_in_the_json(self):
        # host null : config.load().host vaut None, pas le nom machine.
        (self.state / 'scenario').write_text('identity-error\n')
        machine = self.machine_host()
        self.point_bascule_at(machine)
        self.runner_env()
        (self.vmhome / '.config/ameesh/config.json').write_text(
            json.dumps({'host': None}))
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('identité', p.stderr)
        self.assertEqual(self.pip_calls(), [])

    def test_identity_fallback_reads_the_legacy_config(self):
        (self.state / 'scenario').write_text('identity-error\n')
        self.point_bascule_at(self.machine_host())
        self.runner_env()
        legacy = self.vmhome / '.config/agent-mesh'
        legacy.mkdir(parents=True)
        (legacy / 'config.json').write_text(json.dumps({'host': 'legacy-wrong-host'}))
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('legacy-wrong-host', p.stderr)
        self.assertEqual(self.pip_calls(), [])

    def test_identity_fallback_reads_the_ameesh_config_pointer(self):
        (self.state / 'scenario').write_text('identity-error\n')
        self.point_bascule_at(self.machine_host())
        ailleurs = self.vmhome / '.config/ameesh/ailleurs.json'
        ailleurs.write_text(json.dumps({'host': 'pointer-wrong-host'}))
        self.runner_env(AMEESH_CONFIG=str(ailleurs))
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('pointer-wrong-host', p.stderr)
        self.assertEqual(self.pip_calls(), [])

    def test_identity_fallback_refuses_an_unreadable_config(self):
        # Un fichier présent mais illisible ne doit pas retomber sur le nom
        # machine : l'installation reste fermée avant toute mutation.
        (self.state / 'scenario').write_text('identity-error\n')
        self.point_bascule_at(self.machine_host())
        self.runner_env()
        (self.vmhome / '.config/ameesh/config.json').write_text('{pas du json\n')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('identité', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertEqual(self.pip_calls(), [])

    def _pristine_snapshot(self, sha):
        """An unmodified src-<sha7> clone, as a previous run would leave one."""
        snapshot = self.parts / ('src-' + sha[:7])
        subprocess.run(['git', 'clone', '-q', '--shared', '--no-checkout',
                        str(self.origin), str(snapshot)], check=True, timeout=300)
        subprocess.run(['git', '-C', str(snapshot), 'checkout', '-q',
                        '--detach', sha], check=True, timeout=120)
        return snapshot

    def test_install_reuses_a_pristine_existing_snapshot_without_overwriting_it(self):
        target_snapshot = self._pristine_snapshot(self.target)
        inode = target_snapshot.stat().st_ino
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(target_snapshot.stat().st_ino, inode)
        self.assertEqual(self.head(target_snapshot), self.target)
        for call in self.pip_calls():
            self.assertIn('src-' + self.target[:7], call)

    def test_install_refuses_an_altered_existing_snapshot_before_any_pip(self):
        target_snapshot = self._pristine_snapshot(self.target)
        (target_snapshot / 'src/ameesh/__init__.py').write_text('# altéré\n')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('instantané', p.stderr)
        self.assertIn('contenu modifié', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertFalse((self.parts / 'installation-en-cours').exists())
        self.assertEqual(self.pip_calls(), [])
        self.assertEqual((target_snapshot / 'src/ameesh/__init__.py').read_text(),
                         '# altéré\n')

    def test_install_refuses_an_added_file_in_an_existing_snapshot(self):
        target_snapshot = self._pristine_snapshot(self.target)
        (target_snapshot / 'sitecustomize.py').write_text('# ajouté\n')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('fichier en trop', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertEqual(self.pip_calls(), [])

    def test_install_refuses_a_source_masked_by_assume_unchanged(self):
        # `git status` est vide (index masqué) : la vérification par objets du
        # commit doit voir le fichier modifié sur le disque.
        target_snapshot = self._pristine_snapshot(self.target)
        self.mask_tracked_source(target_snapshot, '--assume-unchanged')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('contenu modifié', p.stderr)
        self.assertEqual(self.pip_calls(), [])
        self.assertEqual(self.head(self.src), self.old)
        self.assertEqual((target_snapshot / 'src/ameesh/__init__.py').read_text(),
                         '# modifié sous --assume-unchanged\n')

    def test_install_refuses_a_source_masked_by_skip_worktree(self):
        target_snapshot = self._pristine_snapshot(self.target)
        self.mask_tracked_source(target_snapshot, '--skip-worktree')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('contenu modifié', p.stderr)
        self.assertEqual(self.pip_calls(), [])
        self.assertEqual(self.head(self.src), self.old)

    def test_install_refuses_a_type_change_in_a_snapshot(self):
        # Fichier suivi remplacé par un lien : le type fait partie de l'arbre.
        target_snapshot = self._pristine_snapshot(self.target)
        cible = target_snapshot / 'src/ameesh/__init__.py'
        cible.unlink()
        cible.symlink_to('/dev/null')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('type différent du commit', p.stderr)
        self.assertEqual(self.pip_calls(), [])
        self.assertEqual(self.head(self.src), self.old)

    def test_install_normalizes_ignored_build_artifacts_before_reusing_a_snapshot(self):
        # `build/` est ignoré par le dépôt mais consommé par le build suivant :
        # l'instantané est nettoyé avant réutilisation, rien d'ajouté n'entre.
        target_snapshot = self._pristine_snapshot(self.target)
        planted = target_snapshot / 'build/lib/ameesh/evil.py'
        planted.parent.mkdir(parents=True)
        planted.write_text('# ajouté\n')
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(planted.exists())
        self.assertEqual(self.head(target_snapshot), self.target)
        for call in self.pip_calls():
            self.assertIn('src-' + self.target[:7], call)

    def test_failed_install_keeps_previous_sha_and_retour_returns_to_the_left_code(self):
        plus_ancien = self.plus_ancien
        (self.parts / 'previous-sha').write_text(plus_ancien + '\n')
        (self.state / 'pip-fail-at').write_text('2\n')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('journal', p.stderr)
        self.assertEqual((self.parts / 'previous-sha').read_text().strip(), plus_ancien)
        journal = (self.parts / 'installation-en-cours').read_text()
        self.assertIn('sha=' + self.old, journal)
        self.assertEqual(self.head(self.src), self.target)
        self.assertEqual(len(self.pip_calls()), 2)
        # Le retour suit le journal (code quitté = old), pas previous-sha.
        (self.state / 'pip-fail-at').unlink()
        r = self.run_vm('retour')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertFalse((self.parts / 'installation-en-cours').exists())
        self.assertIn('dev9 : redémarré', r.stdout)
        self.assertIn('contrôles verts', r.stdout)
        for call in self.pip_calls()[2:]:
            self.assertIn('src-' + self.old[:7], call)
            self.assertNotIn('src-' + plus_ancien[:7], call)

    def test_failed_first_install_leaves_a_journal_and_retour_returns_to_the_left_code(self):
        # Première utilisation : aucun previous-sha, le journal seul permet le retour.
        (self.parts / 'previous-sha').unlink()
        (self.state / 'pip-fail-at').write_text('2\n')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('sha=' + self.old, (self.parts / 'installation-en-cours').read_text())
        self.assertFalse((self.parts / 'previous-sha').exists())
        (self.state / 'pip-fail-at').unlink()
        r = self.run_vm('retour')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertEqual((self.parts / 'previous-sha').read_text().strip(), self.old)

    # -- journal de récupération : relance d'installer (B1) ------------------
    def test_install_refuses_while_a_recovery_journal_exists(self):
        (self.parts / 'installation-en-cours').write_text(
            'sha=%s\ninstantane=src-%s\ncible=%s\n'
            % (self.old, self.old[:7], self.target))
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('installation inachevée', p.stderr)
        self.assertIn('retour', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertFalse((self.parts / ('src-' + self.target[:7])).exists())
        self.assertEqual(self.pip_calls(), [])
        self.assertIn('sha=' + self.old,
                      (self.parts / 'installation-en-cours').read_text())

    def test_install_refuses_an_invalid_recovery_journal_before_any_mutation(self):
        (self.parts / 'installation-en-cours').write_text('sha=pas-un-sha\n')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('journal de récupération invalide', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertFalse((self.parts / ('src-' + self.target[:7])).exists())
        self.assertEqual(self.pip_calls(), [])

    def test_relaunching_install_keeps_the_original_recovery_target(self):
        # A→B, le premier pip réussit puis le second échoue : le journal vaut A,
        # mais direct_url.json pointe déjà B. Relancer installer ne doit pas
        # prendre B pour cible de récupération ni écraser le journal.
        plus_ancien = self.plus_ancien
        (self.parts / 'previous-sha').write_text(plus_ancien + '\n')
        (self.state / 'pip-fail-at').write_text('2\n')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(len(self.pip_calls()), 2)
        self.assertEqual(self.installed_url(),
                         (self.parts / ('src-' + self.target[:7])).as_uri())
        q = self.run_vm('installer', self.target)
        self.assertNotEqual(q.returncode, 0)
        self.assertIn('installation inachevée', q.stderr)
        self.assertEqual(len(self.pip_calls()), 2)
        self.assertIn('sha=' + self.old,
                      (self.parts / 'installation-en-cours').read_text())
        (self.state / 'pip-fail-at').unlink()
        r = self.run_vm('retour')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertFalse((self.parts / 'installation-en-cours').exists())
        for call in self.pip_calls()[2:]:
            self.assertIn('src-' + self.old[:7], call)
            self.assertNotIn('src-' + self.target[:7], call)

    def test_relaunching_first_install_keeps_the_recovery_journal(self):
        # Même cas sans previous-sha : le journal seul porte la récupération.
        (self.parts / 'previous-sha').unlink()
        (self.state / 'pip-fail-at').write_text('2\n')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.installed_url(),
                         (self.parts / ('src-' + self.target[:7])).as_uri())
        q = self.run_vm('installer', self.target)
        self.assertNotEqual(q.returncode, 0)
        self.assertEqual(len(self.pip_calls()), 2)
        self.assertIn('sha=' + self.old,
                      (self.parts / 'installation-en-cours').read_text())
        (self.state / 'pip-fail-at').unlink()
        r = self.run_vm('retour')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertEqual((self.parts / 'previous-sha').read_text().strip(), self.old)

    def test_relaunching_install_on_the_installed_sha_keeps_the_return_target(self):
        # A→B réussi (previous-sha = A), puis relance de « installer B » : la
        # cible de retour reste A. Sans la garde, previous-sha serait réécrit à
        # B et « retour » reviendrait sur place — un faux retour (#1252 R2).
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.installed_url(),
                         (self.parts / ('src-' + self.target[:7])).as_uri())
        self.assertEqual((self.parts / 'previous-sha').read_text().strip(), self.old)
        q = self.run_vm('installer', self.target)
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertIn('previous-sha inchangé', q.stdout)
        self.assertEqual((self.parts / 'previous-sha').read_text().strip(), self.old)
        r = self.run_vm('retour')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head(self.src), self.old)

    def test_relaunch_on_installed_sha_without_previous_sha_invents_none(self):
        # Code déjà en place sans previous-sha : la relance n'en invente pas un
        # qui pointerait le code courant ; « retour » refuse au lieu de mentir.
        (self.parts / 'previous-sha').unlink()
        self._write_direct_url(self.target)
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse((self.parts / 'previous-sha').exists())
        r = self.run_vm('retour')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('aucun retour possible', r.stderr)

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

    def test_retour_refuses_when_the_runner_host_is_not_hote_agents(self):
        (self.state / 'expected-env.json').write_text(json.dumps(
            dict(AMEESH_DSN='postgresql://test@invalid/mesh', AMEESH_SCHEMA='test',
                 AMEESH_HOST='wrong-host')))
        p = self.run_vm('retour')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('HOTE_AGENTS', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertEqual(self.pip_calls(), [])

    def test_retour_refuses_an_altered_snapshot_before_any_pip(self):
        (self.parts / 'previous-sha').write_text(self.old + '\n')
        old_snapshot = self._pristine_snapshot(self.old)
        (old_snapshot / 'src/ameesh/__init__.py').write_text('# altéré\n')
        p = self.run_vm('retour')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('contenu modifié', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertEqual(self.pip_calls(), [])

    def test_retour_refuses_a_source_masked_by_assume_unchanged(self):
        (self.parts / 'previous-sha').write_text(self.old + '\n')
        old_snapshot = self._pristine_snapshot(self.old)
        self.mask_tracked_source(old_snapshot, '--assume-unchanged')
        p = self.run_vm('retour')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('contenu modifié', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertEqual(self.pip_calls(), [])

    def test_retour_refuses_an_invalid_journal(self):
        (self.parts / 'installation-en-cours').write_text('sha=pas-un-sha\n')
        p = self.run_vm('retour')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('journal de récupération invalide', p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        self.assertEqual(self.pip_calls(), [])

    def test_retour_reinstalls_the_previous_code_then_restarts_and_controls(self):
        self._clone_at_old(self.target)
        self._write_direct_url(self.target)
        (self.parts / 'previous-sha').write_text(self.old + '\n')
        p = self.run_vm('retour')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.head(self.src), self.old)
        snapshot = self.parts / ('src-' + self.old[:7])
        self.assertEqual(self.head(snapshot), self.old)
        for call in self.pip_calls():
            self.assertIn('src-' + self.old[:7], call)
        self.assertIn('--user stop ameesh-runner-agent@dev9.service',
                      (self.state / 'systemctl.log').read_text())
        self.assertIn('--user start ameesh-runner-agent@dev9.service',
                      (self.state / 'systemctl.log').read_text())
        self.assertNotIn('restart', (self.state / 'systemctl.log').read_text())
        self.assertIn('dev9 : redémarré', p.stdout)
        self.assertIn('contrôles verts', p.stdout)
        # second retour : idempotent, la cible est le code installé
        q = self.run_vm('retour')
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertEqual(self.head(self.src), self.old)

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
        # Le poste de travail est le dépôt d'origine bâti par le harnais : les
        # trois commits (previous, mid, target) viennent du test, jamais de
        # l'historique du checkout.
        self.depot = self.origin
        self.mid = self.old
        self.old = self.plus_ancien
        # The VM clone holds the old commit only, and has no origin at all.
        shutil.rmtree(self.src)
        subprocess.run(['git', 'init', '-q', str(self.src)], check=True)
        subprocess.run(['git', '-C', str(self.src), 'fetch', '-q', str(self.depot), self.old],
                       check=True, timeout=120)
        subprocess.run(['git', '-C', str(self.src), 'checkout', '-q', '--detach', self.old],
                       check=True)
        self._write_direct_url(self.old)
        (self.parts / 'previous-sha').write_text(self.old + '\n')
        (self.state / 'versions.txt').write_text('1\n2\n3\n')

    def _origin_main(self):
        """The workstation's origin/main, the base the old bundle excluded."""
        subprocess.run(['git', '-C', str(self.depot), 'update-ref',
                        'refs/remotes/origin/main', self.target], check=True)

    def test_install_sends_a_git_bundle_when_the_vm_lacks_the_commit(self):
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((self.state / 'scp.log').exists(), p.stdout)
        self.assertIn('ameesh.bundle', (self.state / 'scp.log').read_text())
        self.assertEqual(self.head(self.src), self.target)
        self.assertEqual(self.head(self.parts / ('src-' + self.target[:7])), self.target)
        self.assertEqual((self.parts / 'previous-sha').read_text().strip(), self.old)
        self.assertFalse(list(self.vmtmp.glob('ameesh.bundle*')))

    def test_bundle_lands_on_a_private_temp_name_made_readable(self):
        # Le nom distant n'est pas le nom fixe /tmp/ameesh.bundle (pré-création
        # ou symlink exploitable) et le script rend la cible lisible APRÈS le
        # transfert : le scp simulé la crée en 0600 (umask restrictif du root
        # distant), le chmod 644 du script la remet en 0644 pour ameesh.
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        scp = (self.state / 'scp.log').read_text().strip().splitlines()[-1]
        dest = scp.rsplit(':', 1)[1]
        nom = dest.rsplit('/', 1)[1]
        self.assertTrue(nom.startswith('ameesh.bundle.'), dest)
        mirroir = self.vmtmp / nom
        ssh_log = (self.state / 'ssh.log').read_text()
        self.assertIn("chmod 644 '%s'" % mirroir, ssh_log)
        self.assertFalse(list(self.vmtmp.glob('ameesh.bundle*')), ssh_log)

    def test_install_sends_a_bundle_for_a_target_already_merged_into_main(self):
        # Cible déjà fusionnée : l'ancien ^origin/main rendait le paquet vide.
        self._origin_main()
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('ameesh.bundle', (self.state / 'scp.log').read_text())
        self.assertEqual(self.head(self.src), self.target)
        self.assertEqual((self.parts / 'previous-sha').read_text().strip(), self.old)

    def test_retour_sends_a_bundle_for_an_ancestor_of_main_missing_on_the_vm(self):
        # Ancêtre de main visé pour le retour, absent de la VM : même cas vide.
        self._origin_main()
        (self.parts / 'previous-sha').write_text(self.mid + '\n')
        p = self.run_vm('retour')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('ameesh.bundle', (self.state / 'scp.log').read_text())
        self.assertEqual(self.head(self.src), self.mid)
        for call in self.pip_calls():
            self.assertIn('src-' + self.mid[:7], call)


if __name__ == '__main__':
    unittest.main()
