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
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy/mise-a-jour'

#: variables du runner qui supplanteraient le HOME, la config ou l'etat
#: temporaires : jamais heritees par le banc (incident du 14/10 02:0xZ,
#: consigne mesh-design 02:21).
ENV_PREFIXES = ('AMEESH_', 'AGENT_MESH_', 'AGENT_MAIL_')
ENV_EXTRA = ('XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME')
#: DSN volontairement injoignable : une commande qui sort des stubs echoue
#: immediatement au lieu d'ecrire sur la base commune du mesh.
DSN_REFUSE = 'postgresql://refuse:refuse@127.0.0.1:1/refuse_1155'
#: variables du bac a sable : les seules AMEESH_*/AGENT_* admises dans l'env.
ENV_BAC = ('AMEESH_CONFIG', 'AMEESH_DSN', 'AGENT_MESH_DSN', 'AMEESH_SCHEMA',
           'AGENT_MESH_SCHEMA', 'AMEESH_STATE', 'AGENT_MESH_STATE',
           'AMEESH_THREADS', 'AGENT_MAIL_STATE', 'AGENT_MAIL_CONFIG')


def env_isole(environ=None):
    """Environnement du banc, debarrasse des reglages du runner qui
    supplanteraient le HOME, la config, l'etat ou le DSN reels (incident du
    14/10 02:0xZ ; replis AGENT_MESH_CONFIG, config historique et alias
    ``*_DATABASE_URL`` releves par l'auditeur le 14/10 02:26)."""
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items()
            if not k.startswith(ENV_PREFIXES) and k not in ENV_EXTRA}

#: a stub deploy script: logs the call, fails once on a scenario line
STUB = r'''#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_STATE:?}
printf 'stub %s %s\n' "$0" "$*" >> "$state/calls.log"
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
printf 'stub %s %s\n' "$0" "$*" >> "$state/calls.log"
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
printf 'stub %s %s\n' "$0" "$*" >> "$state/calls.log"
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
printf 'stub %s %s\n' "$0" "$*" >> "${FAKE_STATE:?}/calls.log"
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
        base = env_isole()
        bacs = {}
        for nom in ('etat', 'fils', 'v0', 'config', 'tmp'):
            bacs[nom] = self.root / nom
            bacs[nom].mkdir()
        self.env = dict(base, HOME=str(self.home), BASCULE_ENV=str(self.config),
                        FAKE_STATE=str(self.state),
                        PATH=str(self.bin) + os.pathsep + base.get('PATH', ''),
                        TMPDIR=str(bacs['tmp']),
                        # jamais la configuration, l'etat ni le DSN reels : les
                        # replis de config.load() (AGENT_MESH_CONFIG, alias
                        # *_DATABASE_URL) sont retires par le filtre ; le chemin
                        # explicite absent est en plus refuse avant connexion
                        # (#1127), le DSN restant est injoignable, et toute
                        # ecriture irait sous le bac (incident du 14/10 02:0xZ ;
                        # consigne mesh-design 02:21 ; precision auditeur 02:26).
                        AMEESH_CONFIG=str(self.root / 'config-absente.json'),
                        AMEESH_STATE=str(bacs['etat']),
                        AGENT_MESH_STATE=str(bacs['etat']),
                        AMEESH_THREADS=str(bacs['fils']),
                        AGENT_MAIL_STATE=str(bacs['v0']),
                        AGENT_MAIL_CONFIG=str(bacs['config']),
                        AMEESH_DSN=DSN_REFUSE, AGENT_MESH_DSN=DSN_REFUSE,
                        AMEESH_SCHEMA='refuse_1155', AGENT_MESH_SCHEMA='refuse_1155',
                        GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')

    def git_env(self):
        return dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                    GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')

    def run_staging(self, *args, env=None):
        env = env or self.env
        # garde-fous : le staging de test ne doit jamais lire le HOME, la
        # config, l'etat ni le DSN reels.
        self.assertEqual(env.get('HOME'), str(self.home))
        self.assertFalse(Path(env.get('AMEESH_CONFIG', '')).exists())
        for nom in ('AMEESH_STATE', 'AGENT_MESH_STATE', 'AMEESH_THREADS',
                    'AGENT_MAIL_STATE', 'AGENT_MAIL_CONFIG'):
            self.assertTrue(str(env.get(nom, '')).startswith(str(self.root)), nom)
        self.assertEqual(env.get('AMEESH_DSN'), DSN_REFUSE)
        p = subprocess.run(['bash', str(self.script_dir / 'staging.sh'), *args],
                           env=env, capture_output=True, text=True,
                           timeout=180)
        self.assert_no_escape()
        return p

    def assert_no_escape(self):
        """Tout executable trace doit etre un stub du bac a sable : le banc
        echoue si une commande sort des stubs ou de la config temporaire."""
        for line in self.calls():
            if line.startswith('stub '):
                chemin = line.split(' ', 2)[1]
                self.assertTrue(chemin.startswith(str(self.root)),
                                'executable hors bac a sable : %s' % line)

    def test_environment_is_isolated(self):
        """Aucun reglage herite du runner ne peut atteindre le mesh reel."""
        env = self.env
        for nom in ENV_BAC:
            self.assertIn(nom, env, nom)
        self.assertFalse(Path(env['AMEESH_CONFIG']).exists())
        for nom in ('AMEESH_STATE', 'AGENT_MESH_STATE', 'AMEESH_THREADS',
                    'AGENT_MAIL_STATE', 'AGENT_MAIL_CONFIG'):
            self.assertTrue(env[nom].startswith(str(self.root)), nom)
        for nom in ('AMEESH_DSN', 'AGENT_MESH_DSN'):
            self.assertEqual(env[nom], DSN_REFUSE, nom)
        for cle in env:
            if cle.startswith(ENV_PREFIXES):
                self.assertIn(cle, ENV_BAC, 'variable heritee : %s' % cle)

    def test_le_filtre_ecarte_les_replis_du_runner(self):
        """Temoin de non-recidive (#1195) : un banc qui herite du runner garde
        l'alias AGENT_MESH_CONFIG -- qui lit un fichier reel -- et les alias
        ``*_DATABASE_URL`` -- qui supplantent le DSN. Le banc ne les herite plus
        (temoins 1-2 : sans filtre ; temoin 3 : avec). Le chemin explicite
        absent du banc est en plus refuse par config.load avant connexion
        (#1127), et l'alias perime ne fait pas foi devant un fichier nomme."""
        from ameesh import config
        vide = self.root / 'config-vide.json'
        vide.write_text('{}')
        piege = self.root / 'piege.json'
        piege.write_text(json.dumps({'dsn': 'postgresql://piege@127.0.0.1/piege'}))
        # 1. sans AMEESH_CONFIG, l'alias AGENT_MESH_CONFIG lit un fichier reel
        repli = {'AGENT_MESH_CONFIG': str(piege)}
        self.assertEqual(config.load(dict(repli)).dsn,
                         'postgresql://piege@127.0.0.1/piege')
        # 2. les alias *_DATABASE_URL supplantent le DSN, meme quand un fichier
        #    de config vide est nomme explicitement
        alias = {'AMEESH_CONFIG': str(vide),
                 'AMEESH_DATABASE_URL': 'postgresql://alias@127.0.0.1/alias',
                 'AGENT_MESH_DATABASE_URL': 'postgresql://alias@127.0.0.1/alias'}
        self.assertEqual(config.load(dict(alias)).dsn,
                         'postgresql://alias@127.0.0.1/alias')
        # 3. le filtre du banc les ecarte tous
        herite = dict(repli, **alias)
        herite.update({'AMEESH_STATE': '/home/ameesh/.local/state/ameesh',
                       'AGENT_MAIL_STATE': '/home/ameesh/.local/state/agent-mail',
                       'XDG_STATE_HOME': '/home/ameesh/.local/state',
                       'PATH': '/usr/bin'})
        self.assertEqual(env_isole(herite), {'PATH': '/usr/bin'})

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
        # N2 : la trace doit nommer la cible de retour conservee, pas la cible
        # courante.
        journal = self.home / '.local/state/ameesh/mise-a-jour'
        journal.mkdir(parents=True)
        (journal / 'precedent').write_text('src-deadbee\n')
        p = self.run_staging()
        self.assertEqual(p.returncode, 0, p.stderr)
        actions = self.actions()
        self.assertNotIn(('pc-smichea', 'installer'), actions)
        for action in ('role', 'redemarrer', 'controler'):
            self.assertIn(('pc-smichea', action), actions)
        self.assertTrue(any(t['hote'] == 'pc-smichea' and t['etape'] == 'installer'
                            and 'deja installe' in (t['detail'] or '')
                            for t in self.traces()))
        self.assertTrue(any(t['hote'] == 'pc-smichea' and t['etape'] == 'installer'
                            and t['precedent'] == 'src-deadbee'
                            for t in self.traces()))

    def test_subset_keeps_canonical_order_without_poste(self):
        p = self.run_staging('--hotes', 'ameesh-agents-2 ameesh-agents-1', '--sans-sonde')
        self.assertEqual(p.returncode, 0, p.stderr)
        installes = [h for h, a in self.actions() if a == 'installer']
        self.assertEqual(installes, ['ameesh-agents-1', 'ameesh-agents-2'])
        self.assertNotIn(('pc-smichea', 'installer'), self.actions())
        self.assertNotIn(('pc-smichea', 'redemarrer'), self.actions())
        self.assertNotIn('manaty-mesh-1', [h for h, _ in self.actions()])
        migrations = [l for l in self.calls() if 'action=migrer' in l]
        self.assertEqual(len(migrations), 1)
        self.assertIn('--additif-seulement %s' % self.sha, migrations[0])

    def test_subset_with_poste_is_refused(self):
        """canon §14 : le poste ne se met a jour qu'apres les trois VM saines ;
        un sous-ensemble qui l'inclut n'en apporte aucune preuve (avis R1 B4).
        Le refus porte sur la selection normalisee : les doublons ne comptent
        pas, et tabulation/nouvelle ligne ne contournent plus le controle."""
        cas = {
            'poste seul': 'pc-smichea',
            'poste puis une VM': 'pc-smichea ameesh-agents-1',
            'poste repete quatre fois (zero VM)':
                'pc-smichea pc-smichea pc-smichea pc-smichea',
            'VM + tabulation + poste': 'ameesh-agents-1\tpc-smichea',
            'VM + nouvelle ligne + poste': 'ameesh-agents-1\npc-smichea',
        }
        for nom, sous_ensemble in cas.items():
            with self.subTest(nom):
                p = self.run_staging('--hotes', sous_ensemble)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn('sequence complete des quatre hotes', p.stderr)
                self.assertEqual(self.calls(), [])

    def test_subset_deduplicates_host_names(self):
        """Selection unique : un nom repete ne fait pas rejouer l'hote."""
        p = self.run_staging('--hotes',
                             'ameesh-agents-2 ameesh-agents-1 ameesh-agents-2',
                             '--sans-sonde')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        installes = [h for h, a in self.actions() if a == 'installer']
        self.assertEqual(installes, ['ameesh-agents-1', 'ameesh-agents-2'])

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


class PosteMigrerTest(unittest.TestCase):
    """Contrat reel du dispatcher de poste.sh (avis R1 B1/B2) : HOME en bac,
    venv factice, depot git reel local ; aucune base, aucun hote, aucun envoi."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-1155-poste-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git_env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                            GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')
        self.home = self.root / 'home'; self.home.mkdir()
        self.repo = self.root / 'repo'
        script = self.repo / 'deploy/mise-a-jour/poste.sh'
        script.parent.mkdir(parents=True)
        shutil.copy2(DEPLOY / 'poste.sh', script)
        dsn = self.repo / 'src/ameesh/dsn.py'
        dsn.parent.mkdir(parents=True)
        shutil.copy2(ROOT / 'src/ameesh/dsn.py', dsn)
        self.git('init', '-q')
        self.git('config', 'user.email', 't@invalid')
        self.git('config', 'user.name', 't')
        (self.repo / 'marque').write_text('base\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.premier = self.git('rev-parse', 'HEAD').stdout.strip()
        (self.repo / 'marque').write_text('base\ntete\n')
        self.git('add', 'marque')
        self.git('commit', '-qm', 'tete')
        self.tete = self.git('rev-parse', 'HEAD').stdout.strip()
        self.partage = self.home / '.local/share/ameesh'
        self.venv = self.partage / 'venv/bin'
        self.venv.mkdir(parents=True)
        stub = self.venv / 'python'
        stub.write_text('#!/usr/bin/env bash\n'
                        'printf "venv %s | PYTHONPATH=%s\\n" "$*" "${PYTHONPATH:-}" '
                        '>> "${FAKE_LOG:?}"\n')
        stub.chmod(0o755)
        self.log = self.root / 'venv.log'; self.log.touch()
        self.config = self.home / '.config/ameesh/config.json'
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({'dsn': 'postgresql://u:p@127.0.0.1:1/x'}))
        self.bascule = self.root / 'bascule.env'
        self.bascule.write_text('VM=10.77.0.2\n')
        (self.root / 'run').mkdir()
        self.env = dict(self.git_env, HOME=str(self.home), BASCULE_ENV=str(self.bascule),
                        FAKE_LOG=str(self.log), XDG_RUNTIME_DIR=str(self.root / 'run'))

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args],
                              capture_output=True, text=True, check=True, env=self.git_env)

    def run_poste(self, *args):
        self.log.write_text('')
        return subprocess.run(['bash', str(self.repo / 'deploy/mise-a-jour/poste.sh'), *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def test_dispatcher_forwards_the_ref(self):
        """B1 : migrer REF doit migrer REF, pas HEAD du depot."""
        p = self.run_poste('migrer', self.premier)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        lignes = self.log.read_text().splitlines()
        self.assertTrue(any('migrate --dry-run' in l for l in lignes), lignes)
        self.assertTrue(any('migrate --additive-only' in l for l in lignes), lignes)
        self.assertTrue(any(('src-%s' % self.premier[:7]) in l for l in lignes), lignes)
        self.assertFalse(any(('src-%s' % self.tete[:7]) in l for l in lignes), lignes)

    def test_additive_guard_forced_on_a_reprise(self):
        """B2 : sur une cible deja installee, --additif-seulement garde le refus."""
        (self.partage / ('src-%s' % self.tete[:7])).mkdir()
        (self.partage / 'src-current').symlink_to('src-%s' % self.tete[:7])
        p = self.run_poste('migrer', self.tete)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertTrue(any('migrate --additive-only' in l
                            for l in self.log.read_text().splitlines()))
        p = self.run_poste('migrer', '--additif-seulement', self.tete)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertTrue(any('migrate --additive-only' in l
                            for l in self.log.read_text().splitlines()))


class PaquetGitTest(unittest.TestCase):
    """Contrat reel du paquet git (avis R1 B3) : deux depots isoles et un
    destinataire en retard ; l'ancienne exclusion (origin/main) produisait un
    paquet vide quand la cible etait un ancetre de main."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-1155-paquet-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = self.root / 'origin.git'
        self.poste = self.root / 'poste'
        self.vm = self.root / 'vm'
        self.env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')
        self.git(str(self.root), 'init', '-q', '--bare', '-b', 'main', str(self.origin))
        self.git(str(self.root), 'clone', '-q', str(self.origin), str(self.poste))
        self.shas = []
        for contenu in ('a\n', 'b\n', 'c\n'):
            (self.poste / 'marque').write_text(contenu)
            self.git(str(self.poste), 'add', 'marque')
            self.git(str(self.poste), 'commit', '-qm', contenu.strip())
            self.shas.append(self.git(str(self.poste), 'rev-parse', 'HEAD').stdout.strip())
        self.git(str(self.poste), 'push', '-q', 'origin', 'main')
        self.a, self.b, self.c = self.shas
        self.git(str(self.root), 'clone', '-q', str(self.origin), str(self.vm))
        self.git(str(self.vm), 'checkout', '-q', self.a)  # destinataire en retard

    def git(self, cwd, *args, check=True):
        return subprocess.run(['git', '-C', cwd, *args], capture_output=True,
                              text=True, env=self.env, check=check)

    def test_old_exclusion_makes_an_empty_bundle(self):
        out = self.root / 'vide.bundle'
        self.git(str(self.poste), 'update-ref', 'refs/tmp/vide', self.c)
        r = self.git(str(self.poste), 'bundle', 'create', '-q', str(out),
                     'refs/tmp/vide', '^origin/main', check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('empty bundle', r.stderr)

    def test_bundle_for_a_lagging_vm(self):
        out = self.root / 'paquet.bundle'
        r = subprocess.run(['bash', str(DEPLOY / 'paquet-git.sh'), str(self.poste),
                            'refs/mise-a-jour/test', self.c, str(out), self.a],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.git(str(self.vm), 'fetch', '-q', str(out), 'refs/mise-a-jour/test')
        self.git(str(self.vm), 'cat-file', '-e', '%s^{commit}' % self.c)
        r = self.git(str(self.poste), 'rev-parse', '--verify', 'refs/mise-a-jour/test',
                     check=False)
        self.assertNotEqual(r.returncode, 0, 'reference temporaire non retiree')

    def test_bundle_without_base_for_an_empty_repo(self):
        vide = self.root / 'vide-depot'
        self.git(str(self.root), 'init', '-q', '-b', 'main', str(vide))
        out = self.root / 'complet.bundle'
        r = subprocess.run(['bash', str(DEPLOY / 'paquet-git.sh'), str(self.poste),
                            'refs/mise-a-jour/test', self.b, str(out)],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.git(str(vide), 'fetch', '-q', str(out), 'refs/mise-a-jour/test')
        self.git(str(vide), 'cat-file', '-e', '%s^{commit}' % self.b)

    def test_vm_sh_delegates_bundle_creation(self):
        source = (DEPLOY / 'vm.sh').read_text()
        self.assertEqual(source.count('paquet-git.sh'), 2, source)
        self.assertNotIn('bundle create', source)


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

    def test_dead_letters_include_stopped_personas(self):
        """N1 : une boite morte d'une persona arretee de l'hote est rapportee."""
        db = object()
        arrets = [{'name': 'a', 'host': 'h', 'status': 'stopped'},
                  {'name': 'b', 'host': 'autre', 'status': 'idle'}]
        with mock.patch.object(self.mod.registry, 'overview', return_value=arrets), \
             mock.patch.object(self.mod.storage, 'of') as of:
            of.return_value.mailbox.dead_letters.return_value = [
                {'recipient': 'a', 'n': 2}, {'recipient': 'b', 'n': 1}]
            self.assertEqual(self.mod.dead_letters(db, 'h'),
                             [{'destinataire': 'a', 'n': 2}])

    def sonde(self, etat, repond=False, attente=0.2, accuse_remis=False):
        """La sonde sur faux courrier/registre (#1460) : remise a 100.0."""
        envoye = {}

        def send(db, sender, dest, corps, **kw):
            envoye['jeton'] = corps.split('--ack ')[1].split()[0]
            return 7

        def history(db, nom, **kw):
            return [{'sender': nom, 'body': envoye['jeton'],
                     'delivered_ts': 102.0 if accuse_remis else None}] if repond else []

        def unread(db, nom):
            return [m for m in history(db, nom) if not m['delivered_ts']]

        with mock.patch.object(self.mod.mail, 'send', side_effect=send), \
             mock.patch.object(self.mod.mail, 'get', return_value={'delivered_ts': 100.0}), \
             mock.patch.object(self.mod.mail, 'unread', side_effect=unread), \
             mock.patch.object(self.mod.mail, 'history', side_effect=history), \
             mock.patch.object(self.mod.registry, 'get',
                               **({'side_effect': etat} if callable(etat)
                                  else {'return_value': etat})), \
             mock.patch.object(self.mod.time, 'sleep'):
            return self.mod.probe(object(), {'name': 'p', 'host': 'h'}, 'abc', attente)

    def test_probe_ko_when_the_harness_exits_127_after_delivery(self):
        etat = {'last_turn_ts': 101.0, 'status': 'blocked', 'last_error':
                'hote non pret : the harness could not run (code 127)'}
        out = self.sonde(etat, attente=60.0)
        self.assertIsNone(out['delai_s'])
        self.assertIn('code 127', out['tour'])

    def test_probe_delivered_without_tool_answer_is_ko(self):
        out = self.sonde({'last_turn_ts': 99.0, 'status': 'idle'})
        self.assertIsNone(out['delai_s'])
        self.assertEqual(out['tour'], "remis, sans reponse de l'outil")

    def test_probe_ok_only_with_the_answer_and_a_finished_turn(self):
        out = self.sonde({'last_turn_ts': 100.5, 'status': 'idle'}, repond=True)
        self.assertEqual(out['tour'], 'ok')
        self.assertIsNotNone(out['delai_s'])

    def test_probe_ko_when_the_answer_is_seen_but_the_turn_never_ends(self):
        """#1460 : l'outil repond, puis le harnais reste bloque jusqu'au timeout."""
        out = self.sonde({'last_turn_ts': 99.0, 'status': 'running'}, repond=True)
        self.assertIsNone(out['delai_s'])
        self.assertEqual(out['tour'], 'reponse vue, tour non termine')

    def test_probe_ko_when_the_turn_ends_in_error_after_the_answer(self):
        out = self.sonde({'last_turn_ts': 101.0, 'status': 'running',
                          'last_error': 'timeout (124)'}, repond=True)
        self.assertIsNone(out['delai_s'])
        self.assertIn('timeout (124)', out['tour'])

    def test_probe_ko_when_the_turn_fails_between_two_registry_reads(self):
        """Une seule lecture du registre par passage : un tour qui finit en
        erreur entre deux lectures n'est jamais pris pour une fin propre."""
        lectures = iter([{'last_turn_ts': 99.0, 'status': 'running'},
                         {'last_turn_ts': 101.0, 'status': 'blocked',
                          'last_error': 'timeout (124)'}])
        out = self.sonde(lambda db, nom: next(lectures), repond=True, attente=60.0)
        self.assertIsNone(out['delai_s'])
        self.assertIn('timeout (124)', out['tour'])

    def test_probe_ko_when_a_clean_turn_never_answered(self):
        out = self.sonde({'last_turn_ts': 101.0, 'status': 'idle'})
        self.assertIsNone(out['delai_s'])
        self.assertEqual(out['tour'], "remis, sans reponse de l'outil")

    def test_probe_ok_when_the_ack_was_already_delivered(self):
        out = self.sonde({'last_turn_ts': 103.0, 'status': 'idle'},
                         repond=True, accuse_remis=True)
        self.assertEqual(out['tour'], 'ok')
        self.assertIsNotNone(out['delai_s'])

    def test_choose_explicit_target_must_exist(self):
        with self.assertRaises(SystemExit):
            self.mod.choose([{'name': 'a'}], 'inconnu', 1000.0)


if __name__ == '__main__':
    unittest.main()
