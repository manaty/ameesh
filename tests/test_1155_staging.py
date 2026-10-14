# SPDX-License-Identifier: AGPL-3.0-only
"""#1155 staging sequence: four hosts, VM first, workstation last.

The orchestration is exercised against stub `vm.sh`/`poste.sh` and a stub
workstation venv, so no VM, unit, database or real mail is touched.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy/mise-a-jour'

#: a stub deploy script: logs the call, fails once on a scenario line
STUB = r'''#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_STATE:?}
host=pc-smichea
if [ "$(basename "$0")" = vm.sh ]; then
  if [ "${1:-}" = agents ]; then
    host=$(sed -n 's/^HOTE_AGENTS=//p' "$BASCULE_ENV")
    shift
  else
    host=manaty-mesh-1
  fi
fi
action=${1:-}
printf 'call host=%s action=%s args=%s\n' "$host" "$action" "$*" >> "$state/calls.log"
if grep -qx "ko=$host:$action" "$state/scenario" 2>/dev/null; then
  grep -vx "ko=$host:$action" "$state/scenario" > "$state/scenario.tmp" || true
  mv "$state/scenario.tmp" "$state/scenario"
  echo "echec injecte : $host $action" >&2
  exit 1
fi
exit 0
'''

#: the workstation venv, two entry points used by staging.sh
VENV_PYTHON = r'''#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_STATE:?}
printf 'venv-python %s\n' "$*" >> "$state/calls.log"
if [ "${1:-}" = "-" ]; then exit 0; fi   # notification
hote=""; precedent=""
for arg in "$@"; do
  [ "$precedent" = --hote ] && hote=$arg
  precedent=$arg
done
if grep -qx "ko=sante:$hote" "$state/scenario" 2>/dev/null; then
  grep -vx "ko=sante:$hote" "$state/scenario" > "$state/scenario.tmp" || true
  mv "$state/scenario.tmp" "$state/scenario"
  echo "courrier $hote : KO (echec injecte)" >&2
  exit 1
fi
echo "courrier $hote : ok"
'''

VENV_AMEESH = r'''#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_STATE:?}
printf 'venv-ameesh %s\n' "$*" >> "$state/calls.log"
if grep -qx "ko=harness" "$state/scenario" 2>/dev/null; then
  grep -vx "ko=harness" "$state/scenario" > "$state/scenario.tmp" || true
  mv "$state/scenario.tmp" "$state/scenario"
  echo "harness KO" >&2
  exit 1
fi
exit 0
'''

#: read-only ssh probes (installed sha, previous sha) find nothing: the stubs
#: above own every remote gesture.
SSH = r'''#!/usr/bin/env bash
printf 'ssh %s\n' "$*" >> "${FAKE_STATE:?}/calls.log"
exit 255
'''


class StagingTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-1155-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'; self.state.mkdir()
        (self.state / 'calls.log').touch()
        (self.state / 'scenario').write_text('')
        self.home = self.root / 'home'; self.home.mkdir()
        self.repo = self.root / 'repo'
        self.script_dir = self.repo / 'deploy/mise-a-jour'
        self.script_dir.mkdir(parents=True)
        for name in ('staging.sh', 'sante-courrier.py'):
            (self.script_dir / name).write_text((DEPLOY / name).read_text())
        for name, source in (('vm.sh', STUB), ('poste.sh', STUB)):
            path = self.script_dir / name
            path.write_text(source)
            path.chmod(0o755)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        for name in ('ssh',):
            path = self.bin / name
            path.write_text(SSH)
            path.chmod(0o755)
        venv = self.home / '.local/share/ameesh/venv/bin'; venv.mkdir(parents=True)
        for name, source in (('python', VENV_PYTHON), ('ameesh', VENV_AMEESH)):
            path = venv / name
            path.write_text(source)
            path.chmod(0o755)
        self.config = self.root / 'bascule.env'
        self.config.write_text(
            'HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 '
            'manaty-mesh-1=root@10.77.0.2 pc-smichea=poste"\n'
            'VM=10.77.0.2\nPERSONAS_VM=\n')
        self.repo = self.root / 'repo'
        run = ['git', '-C', str(self.repo)]
        subprocess.run(run + ['init', '-q'], check=True, capture_output=True)
        env = self.git_env()
        (self.repo / 'marque').write_text('x\n')
        subprocess.run(run + ['add', 'marque'], check=True, capture_output=True, env=env)
        subprocess.run(run + ['-c', 'user.email=t@invalid', '-c', 'user.name=t',
                              'commit', '-qm', 'base'], check=True, capture_output=True, env=env)
        # un deuxieme commit : la cible du staging doit avoir un parent
        (self.repo / 'marque').write_text('x\ny\n')
        subprocess.run(run + ['add', 'marque'], check=True, capture_output=True, env=env)
        subprocess.run(run + ['-c', 'user.email=t@invalid', '-c', 'user.name=t',
                              'commit', '-qm', 'tete'], check=True, capture_output=True, env=env)
        subprocess.run(run + ['update-ref', 'refs/remotes/origin/main', 'HEAD'],
                       check=True, capture_output=True, env=env)
        self.sha = subprocess.run(run + ['rev-parse', 'HEAD'], check=True,
                                  capture_output=True, text=True).stdout.strip()
        self.env = dict(os.environ, HOME=str(self.home), BASCULE_ENV=str(self.config),
                        FAKE_STATE=str(self.state),
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        # jamais la configuration reelle : si un chemin retombait
                        # sur le venv de l'hote, config.load() echoue au lieu de
                        # lire la base du mesh (incident du 14/10 02:0xZ).
                        AMEESH_CONFIG=str(self.root / 'config-absente.json'),
                        GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')

    def git_env(self):
        return dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                    GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')

    def run_staging(self, *args, env=None):
        env = env or self.env
        # garde-fou : le staging de test ne doit jamais lire le HOME reel
        self.assertEqual(env.get('HOME'), str(self.home))
        self.assertFalse(Path(env.get('AMEESH_CONFIG', '')).exists())
        return subprocess.run(['bash', str(self.script_dir / 'staging.sh'), *args],
                              env=env, capture_output=True, text=True,
                              timeout=180)

    def calls(self):
        return (self.state / 'calls.log').read_text().splitlines()

    def actions(self):
        out = []
        for line in self.calls():
            if line.startswith('call '):
                fields = dict(part.split('=', 1) for part in line[5:].split(' ', 2)[:2])
                out.append((fields['host'], fields['action']))
        return out

    def traces(self):
        journal = self.home / '.local/state/ameesh/mise-a-jour'
        files = sorted(journal.glob('staging-*.jsonl'))
        self.assertEqual(len(files), 1, files)
        return [json.loads(line) for line in files[0].read_text().splitlines()]

    def test_dry_run_touches_nothing(self):
        p = self.run_staging('--a-blanc')
        self.assertEqual(p.returncode, 0, p.stderr)
        for host in ('ameesh-agents-1', 'ameesh-agents-2', 'manaty-mesh-1', 'pc-smichea'):
            self.assertIn(host, p.stdout)
        self.assertEqual(self.calls(), [])
        self.assertIn('par hote installer, redemarrer, controler, sante',
                      p.stdout.splitlines()[-1])

    def test_full_sequence_order_and_trace(self):
        p = self.run_staging()
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(self.actions(), [
            ('ameesh-agents-1', 'verifier'),
            ('ameesh-agents-2', 'verifier'),
            ('manaty-mesh-1', 'verifier'),
            ('pc-smichea', 'verifier'),
            ('pc-smichea', 'sauvegarder'),
            ('pc-smichea', 'migrer'),
            ('ameesh-agents-1', 'installer'),
            ('ameesh-agents-1', 'redemarrer'),
            ('ameesh-agents-1', 'controler'),
            ('ameesh-agents-2', 'installer'),
            ('ameesh-agents-2', 'redemarrer'),
            ('ameesh-agents-2', 'controler'),
            ('manaty-mesh-1', 'installer'),
            ('manaty-mesh-1', 'redemarrer'),
            ('manaty-mesh-1', 'controler'),
            ('pc-smichea', 'installer'),
            ('pc-smichea', 'role'),
            ('pc-smichea', 'redemarrer'),
            ('pc-smichea', 'controler'),
        ])
        self.assertIn('venv-ameesh doctor --harness', self.calls())
        for host in ('ameesh-agents-1', 'ameesh-agents-2', 'manaty-mesh-1', 'pc-smichea'):
            self.assertIn('courrier %s : ok' % host, p.stdout)
        traces = self.traces()
        self.assertEqual(traces[-1]['etape'], 'sequence')
        self.assertEqual(traces[-1]['verdict'], 'ok')
        self.assertEqual(traces[-1]['sha'], self.sha)
        for hote in ('ameesh-agents-1', 'pc-smichea'):
            self.assertTrue(any(t['hote'] == hote and t['etape'] == 'sante'
                                and t['verdict'] == 'ok' for t in traces), traces)
        self.assertEqual((self.home / '.local/state/ameesh/mise-a-jour').stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.script_dir.glob('staging-*.env')), [])

    def test_failure_stops_sequence_and_rolls_back_host(self):
        (self.state / 'scenario').write_text('ko=manaty-mesh-1:controler\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 1, p.stdout)
        actions = self.actions()
        self.assertIn(('manaty-mesh-1', 'retour'), actions)
        self.assertEqual(actions[-1], ('manaty-mesh-1', 'controler'))  # rollback control
        self.assertNotIn(('pc-smichea', 'installer'), actions)
        self.assertNotIn(('manaty-mesh-1', 'sante'), actions)
        self.assertIn('sequence arretee sur manaty-mesh-1', p.stderr)
        notifications = [l for l in self.calls() if l.startswith('venv-python - ')]
        self.assertEqual(len(notifications), 1)
        self.assertIn('mesh-design', notifications[0])
        traces = self.traces()
        self.assertTrue(any(t['hote'] == 'manaty-mesh-1' and t['etape'] == 'controler'
                            and t['verdict'] == 'ko' for t in traces))
        self.assertTrue(any(t['hote'] == 'manaty-mesh-1' and t['etape'] == 'retour'
                            and t['verdict'] == 'ok' for t in traces))
        self.assertTrue(any(t['hote'] == 'manaty-mesh-1' and t['etape'] == 'controle-retour'
                            and t['verdict'] == 'ok' for t in traces))

    def test_failed_rollback_is_reported_and_stops(self):
        (self.state / 'scenario').write_text('ko=sante:ameesh-agents-1\nko=ameesh-agents-1:retour\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 1)
        notifications = [l for l in self.calls() if l.startswith('venv-python - ')]
        self.assertEqual(len(notifications), 1)
        self.assertIn('retour de ameesh-agents-1 en ECHEC', notifications[0])
        self.assertNotIn(('ameesh-agents-2', 'installer'), self.actions())
        self.assertTrue(any(t['hote'] == 'ameesh-agents-1' and t['etape'] == 'retour'
                            and t['verdict'] == 'ko' for t in self.traces()))

    def test_postponed_health_failure_rolls_back_the_workstation(self):
        (self.state / 'scenario').write_text('ko=sante:pc-smichea\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 1)
        self.assertIn(('pc-smichea', 'retour'), self.actions())
        self.assertIn(('pc-smichea', 'controler'), self.actions())

    def test_preflight_failure_changes_nothing(self):
        (self.state / 'scenario').write_text('ko=ameesh-agents-2:verifier\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 1)
        self.assertIn('preflight en echec', p.stderr)
        self.assertEqual(self.actions(), [
            ('ameesh-agents-1', 'verifier'),
            ('ameesh-agents-2', 'verifier'),
        ])
        self.assertNotIn('venv-python', ' '.join(self.calls()))

    def test_migrations_failure_installs_nothing(self):
        (self.state / 'scenario').write_text('ko=pc-smichea:migrer\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 1)
        actions = self.actions()
        self.assertIn(('pc-smichea', 'migrer'), actions)
        self.assertNotIn(('ameesh-agents-1', 'installer'), actions)

    def test_already_installed_workstation_is_not_reinstalled(self):
        court = self.sha[:7]
        (self.home / '.local/share/ameesh').mkdir(parents=True, exist_ok=True)
        (self.home / ('.local/share/ameesh/src-%s' % court)).mkdir()
        (self.home / '.local/share/ameesh/src-current').symlink_to('src-%s' % court)
        p = self.run_staging()
        self.assertEqual(p.returncode, 0, p.stderr)
        actions = self.actions()
        self.assertNotIn(('pc-smichea', 'installer'), actions)
        for action in ('role', 'redemarrer', 'controler'):
            self.assertIn(('pc-smichea', action), actions)
        self.assertTrue(any(t['hote'] == 'pc-smichea' and t['etape'] == 'installer'
                            and 'deja installe' in (t['detail'] or '')
                            for t in self.traces()))

    def test_subset_keeps_canonical_order(self):
        p = self.run_staging('--hotes', 'pc-smichea ameesh-agents-1', '--sans-sonde')
        self.assertEqual(p.returncode, 0, p.stderr)
        hosted = [h for h, _ in self.actions()]
        self.assertEqual(hosted[:4], ['ameesh-agents-1', 'pc-smichea', 'pc-smichea',
                                      'pc-smichea'])
        self.assertNotIn(('ameesh-agents-2', 'verifier'), self.actions())
        self.assertNotIn(('manaty-mesh-1', 'installer'), self.actions())
        self.assertIn(('pc-smichea', 'installer'), self.actions())

    def test_topology_is_required_and_consistent(self):
        cases = {
            'sans topologie': 'VM=10.77.0.2\nPERSONAS_VM=\n',
            'hote inconnu': ('HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 '
                             'manaty-mesh-1=root@10.77.0.2 poste-x=root@10.77.0.9"\nVM=10.77.0.2\n'),
            'vm differente': ('HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 '
                              'manaty-mesh-1=root@10.77.0.2 pc-smichea=poste"\nVM=10.77.0.9\n'),
            'poste absent': ('HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 '
                             'manaty-mesh-1=root@10.77.0.2"\nVM=10.77.0.2\n'),
            'poste distant': ('HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 '
                              'manaty-mesh-1=root@10.77.0.2 pc-smichea=root@10.77.0.5"\nVM=10.77.0.2\n'),
        }
        for nom, contenu in cases.items():
            with self.subTest(nom):
                self.config.write_text(contenu)
                p = self.run_staging()
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertEqual(self.calls(), [])

    def test_outside_main_is_refused_without_override(self):
        self.repo.joinpath('marque').write_text('y\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', 'marque'], check=True,
                       capture_output=True, env=self.git_env())
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.email=t@invalid',
                        '-c', 'user.name=t', 'commit', '-qm', 'next'], check=True,
                       capture_output=True, env=self.git_env())
        p = self.run_staging()
        self.assertEqual(p.returncode, 2)
        self.assertIn("n'est pas un ancetre", p.stderr)
        env = dict(self.env, HORS_MAIN='1')
        p = self.run_staging('--a-blanc', env=env)
        self.assertEqual(p.returncode, 0, p.stderr)


class SanteCourrierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            'sante_courrier', DEPLOY / 'sante-courrier.py')
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_driven_requires_live_lease(self):
        now = 1000.0
        self.assertTrue(self.mod.driven({'lease_owner': 'r', 'lease_expires_ts': now + 5}, now))
        self.assertFalse(self.mod.driven({'lease_owner': 'r', 'lease_expires_ts': now - 5}, now))
        self.assertFalse(self.mod.driven({'lease_owner': None, 'lease_expires_ts': now + 5}, now))
        self.assertFalse(self.mod.driven({}, now))

    def test_choose_auto_takes_first_driven(self):
        now = 1000.0
        rows = [{'name': 'b', 'lease_owner': None, 'lease_expires_ts': None},
                {'name': 'a', 'lease_owner': 'r', 'lease_expires_ts': now + 5},
                {'name': 'c', 'lease_owner': 'r', 'lease_expires_ts': now + 5}]
        self.assertEqual(self.mod.choose(rows, 'auto', now)[0]['name'], 'a')
        self.assertEqual(self.mod.choose(rows, 'jamais', now), (None, 'sonde desactivee'))
        self.assertEqual(self.mod.choose([], 'auto', now)[0], None)

    def test_choose_explicit_target_must_exist(self):
        with self.assertRaises(SystemExit):
            self.mod.choose([{'name': 'a'}], 'inconnu', 1000.0)


if __name__ == '__main__':
    unittest.main()
