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
argv = sys.argv[1:]
command = argv[-1]
# The real client swallows its stdin even when the remote command does not
# read it: a caller that shares its stdin with a `while read` loop loses the
# rest of the list (#1455). Play that, and forward the bytes like ssh does.
# `-n` redirects the client's stdin from /dev/null, as the real one does.
stdin = ''
if '-n' not in argv and not sys.stdin.isatty():
    stdin = sys.stdin.read()
with (state / 'ssh.log').open('a') as f:
    f.write(command + '\n')
command = command.replace('/home/ameesh/.local/share/ameesh/venv/bin/ameesh', os.environ['FAKE_AMEESH'])
command = command.replace('/home/ameesh/.local/share/ameesh/venv/bin/python', os.environ['FAKE_DB_PYTHON'])
command = command.replace('$HOME/.config/ameesh/env', str(state / 'runner.env'))
env = dict(os.environ, FAKE_REMOTE='1')
sys.exit(subprocess.run(['bash', '-c', command], env=env, input=stdin, text=True).returncode)
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
if sys.argv[1:] == ['resume', '--help']:
    # #2152 / R1 B2 : le script ne draine que si la reprise conditionnelle
    # existe côté cible (--expect-reason). Le drapeau simule un CLI antérieur.
    if (state / 'sans-expect-reason').exists():
        print('usage: ameesh resume ... --operator --reason R')
    else:
        print('usage: ameesh resume ... --operator --reason R --expect-reason TEXT')
    sys.exit(0)
if sys.argv[1:] == ['list', '--json']:
    if scenario == 'list-error': sys.exit(1)
    if scenario == 'bad-json': print('not json'); sys.exit(0)
    print((state / 'agents.json').read_text())
elif sys.argv[1] == 'show':
    name = sys.argv[2]
    row = next(a for a in json.loads((state / 'agents.json').read_text()) if a['name'] == name)
    row = dict(row, lease_owner='runner-' + name + '@host-agents', lease_epoch=7, lease_expires_ts=time.time()+300)
    pause = state / ('pause-' + name)
    motif = state / ('motif-' + name)
    if scenario == 'pause-coupee-etat-perdu' and pause.exists() \
            and not (state / 'lecture-perdue').exists():
        (state / 'lecture-perdue').touch()
        sys.exit(1)
    # R1 B1 : après notre pose et avant notre reprise, le monde extérieur lève
    # notre drain puis pose une NOUVELLE pause (humaine, étrangère).
    if scenario == 'drain-remplace' and motif.exists() and '#2152' in motif.read_text() \
            and not (state / 'remplace-fait').exists():
        motif.write_text('pause humaine etrangere')
        (state / 'remplace-fait').touch()
    # N5 B2 : levée humaine de notre premier drain, l'agent restant en tour :
    # le second drain doit seul rester suivi (aucune entrée périmée).
    if scenario == 'chaines-relance' and motif.exists() and '#2152' in motif.read_text() \
            and not (state / 'levee-humaine').exists():
        pause.unlink(); motif.unlink(missing_ok=True)
        (state / 'levee-humaine').touch()
    if scenario == 'expired': row['lease_expires_ts'] = time.time()-1
    if scenario == 'no-lease': row['lease_owner'] = None
    if scenario == 'foreign-owner': row['lease_owner'] = 'another-runner'
    if scenario == 'default-owner': row['lease_owner'] = 'host-agents:123'
    if scenario == 'db-ahead': row['lease_expires_ts'] = time.time()+30
    if scenario == 'moved-before': row['host'] = 'elsewhere'
    if scenario == 'always-busy': row['status'] = 'running'
    # #2152 : un agent qui enchaîne les tours ne rend la main qu'une fois
    # drainé ; « chaines-toujours » ne la rend jamais.
    if name == 'dev9' and scenario in ('chaines', 'chaines-pause-error',
                                       'chaines-resume-error', 'pause-concurrente',
                                       'drain-remplace', 'pause-reponse-perdue',
                                       'pause-coupee', 'pause-coupee-etat-perdu',
                                       'pause-perdue-sans-pose',
                                       'drain-refuse-identite') \
            and not pause.exists():
        row['status'] = 'running'
    if name == 'dev9' and scenario in ('chaines-toujours', 'chaines-relance'):
        row['status'] = 'running'
    if pause.exists():
        row['operator_paused'] = True
        row['operator_pause_reason'] = motif.read_text() if motif.exists() else ''
    if (state / ('restart-' + name)).exists():
        row['lease_epoch'] = 9 if scenario != 'stale-lease' else 7
        if scenario == 'moved': row['host'] = 'elsewhere'
    if scenario == 'busy-once' and name == 'dev9' and not (state / 'busy-seen').exists():
        (state / 'busy-seen').touch(); row['status'] = 'running'
    print(json.dumps(row))
elif sys.argv[1] in ('pause', 'resume'):
    # #2152 : les gestes du drain, avec le « changed » que lit le script.
    name = sys.argv[2]
    args = sys.argv[3:]
    pause = state / ('pause-' + name)
    motif = state / ('motif-' + name)
    with (state / 'operateur.log').open('a') as f:
        f.write('%s %s %s\n' % (sys.argv[1], name, ' '.join(args)))
    if '--reason' not in args: sys.exit(2)
    if sys.argv[1] == 'pause':
        if scenario == 'chaines-pause-error': sys.exit(1)
        if scenario == 'drain-refuse-identite':
            # Geste d'opérateur refusé : vm.sh tourne dans une session liée à
            # un agent (bad_actor), le CLI refuse AVANT toute mutation.
            print('ameesh operator pause: bad_actor: an agent session cannot act '
                  'as the local human', file=sys.stderr)
            sys.exit(1)
        if scenario == 'pause-perdue-sans-pose':
            # R1 B2 : réponse perdue, la pose n'a PAS abouti.
            print('{bad-json')
            sys.exit(0)
        motif.write_text(args[args.index('--reason') + 1])
        pause.touch()
        if scenario == 'pause-concurrente':
            # Posée entre notre lecture et notre pause : elle n'est pas de nous.
            motif.write_text('pause humaine etrangere')
            change = False
        elif scenario == 'pause-reponse-perdue':
            # R1 B2 : la pose a abouti, mais la réponse est illisible.
            print('{bad-json')
            sys.exit(0)
        elif scenario in ('pause-coupee', 'pause-coupee-etat-perdu'):
            # R1 B2 : la pose a abouti, puis le lien SSH tombe (rc ≠ 0).
            sys.exit(1)
        else:
            change = True
        print(json.dumps({'agent': name, 'mode': 'execute', 'operator_paused': True,
                          'changed': change, 'event_id': 1}))
    else:
        if '--operator' not in args: sys.exit(2)
        if (state / 'sans-expect-reason').exists() and '--expect-reason' in args:
            sys.exit(2)
        if scenario == 'chaines-resume-error': sys.exit(1)
        attendu = args[args.index('--expect-reason') + 1] if '--expect-reason' in args else None
        raison = motif.read_text() if motif.exists() else ''
        if pause.exists() and (attendu is None or raison == attendu):
            pause.unlink(); motif.unlink(missing_ok=True)
            print(json.dumps({'agent': name, 'mode': 'execute', 'operator_paused': False,
                              'changed': True, 'event_id': 2}))
        else:
            print(json.dumps({'agent': name, 'mode': 'execute',
                              'operator_paused': pause.exists(), 'changed': False,
                              'event_id': None}))
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
    if scenario == 'inactive-dev9' and '@dev9.' in args[-1]: sys.exit(3)
    if (state / ('inactive-' + unit.split('@')[1].removesuffix('.service'))).exists(): sys.exit(3)
    print('active')
'''

SYSTEMD_RUN = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
# #1133 : the value given to -p must already be absolute — systemd-run does
# not expand %h in a transient unit's properties (it answers "not absolute").
assert args[:7] == ['--user', '--pipe', '--wait', '--quiet', '-p',
                    'EnvironmentFile=' + str(state / 'runner.env'), '--'], args
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

#: #2152 : le drain se décide sur l'horloge réelle (SECONDS de bash). Seule
#: l'attente de la boucle de redémarrage (15 s) fait avancer le banc d'une
#: demi-seconde ; les attentes de bail (5 s) restent instantanées.
SLEEP = ('#!/bin/sh\n'
         'if [ "$1" = 15 ]; then\n'
         '  if [ -n "${HORLOGE_FICHIER:-}" ]; then\n'
         '    echo "$(( $(cat "$HORLOGE_FICHIER") + 2 ))" > "$HORLOGE_FICHIER"\n'
         '  fi\n'
         '  exec %s 0.5\n'
         'fi\n'
         'exit 0\n') % (shutil.which('sleep') or '/bin/sleep')


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
                             ('systemctl', SYSTEMCTL), ('systemd-run', SYSTEMD_RUN), ('db-python', DB_PYTHON), ('sleep', SLEEP),
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

    def operateur(self):
        """#2152 : les gestes de pause/reprise réellement demandés au CLI."""
        path = self.state / 'operateur.log'
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
        log = (self.state / 'systemd-run.log').read_text()
        # #1133 : an absolute path, expanded before systemd-run sees it (the
        # `%h` specifier is not expanded there and would fail the unit).
        self.assertIn('EnvironmentFile=' + str(self.state / 'runner.env'), log)
        self.assertNotIn('%h', log)

    def test_restart_uses_remote_registry_and_user_units(self):
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        restarts = [l for l in self.units().splitlines() if 'restart' in l]
        # #1633 : la persona arrêtée du registre est du lot — son unité active
        # est rechargée sur le nouveau code (aucun bail exigé) ; l'observateur
        # et l'agent d'un autre hôte restent ignorés.
        self.assertEqual(restarts, ['--user restart ameesh-runner-agent@dev8.service',
                                    '--user restart ameesh-runner-agent@dev9.service',
                                    '--user restart ameesh-runner-agent@stopped.service'])
        self.assertIn('dev9 : redémarré', p.stdout)
        self.assertIn('stopped : arrêté dans le registre', p.stdout)
        self.assertNotIn('stopped : redémarré (était', p.stdout)
        self.assertNotIn('no lease before the restart', p.stdout)

    def test_restart_leaves_an_inactive_stopped_unit_alone(self):
        # #1633 : unité déjà arrêtée — aucune relance : elle chargera le
        # nouveau code à son démarrage.
        (self.state / 'inactive-stopped').touch()
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('restart ameesh-runner-agent@stopped.service', self.units())
        self.assertIn('stopped : arrêté dans le registre', p.stdout)
        self.assertIn('déjà arrêtée', p.stdout)

    def test_restart_waits_for_agent_out_of_turn(self):
        p = self.run_vm(scenario='busy-once')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((self.state / 'busy-seen').exists())
        # Un tour qui finit tout seul ne déclenche aucun drain.
        self.assertEqual(self.operateur(), '')

    def test_drain_poses_and_lifts_a_short_operator_pause(self):
        """#2152 : un agent qui enchaîne les tours est drainé (pause opérateur
        courte), redémarré hors tour, puis la pause est levée et prouvée."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines')
        self.assertEqual(p.returncode, 0, p.stderr)
        journal = self.operateur()
        self.assertIn('pause dev9', journal)
        self.assertIn('resume dev9', journal)
        # Le motif de la pause nomme le drain (journalisé par le mesh).
        self.assertIn('drain de déploiement #2152', journal)
        self.assertIn('--reason', journal)
        # #2152 / R1 B1 : la levée est conditionnée au motif exact de la pose.
        pose = [l for l in journal.splitlines() if l.startswith('pause dev9')][0]
        reprise = [l for l in journal.splitlines() if l.startswith('resume dev9')][0]
        motif = pose.split('--reason ', 1)[1]
        self.assertIn('--expect-reason ' + motif, reprise)
        # Posé avant le redémarrage, et redémarré seulement hors tour.
        self.assertLess(p.stdout.index('drain posé'), p.stdout.index('dev9 : redémarré'))
        self.assertIn('dev9 : redémarré (était idle)', p.stdout)
        self.assertIn('drain levé (redémarrage hors tour)', p.stdout)
        self.assertIn('reprise prouvée (operator_paused=false)', p.stdout)
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_foreign_pause_replacing_the_drain_is_not_lifted(self):
        """R1 B1 : notre drain est levé de l'extérieur puis une NOUVELLE pause
        étrangère est posée ; la reprise conditionnelle ne lève que la nôtre."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='drain-remplace')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('pause dev9', self.operateur())
        self.assertIn('--expect-reason', self.operateur())
        self.assertTrue((self.state / 'remplace-fait').exists())
        # La pause étrangère est toujours là, intacte.
        self.assertTrue((self.state / 'pause-dev9').exists())
        self.assertEqual((self.state / 'motif-dev9').read_text(), 'pause humaine etrangere')
        self.assertIn('pause étrangère laissée en place', p.stderr)
        self.assertIn('dev9 : redémarré', p.stdout)

    def test_lost_pause_answer_is_reconciled_by_the_real_state(self):
        """R1 B2 : la pose a abouti mais la réponse est illisible ; l'état réel
        (motif identique) la confirme et le drain continue."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='pause-reponse-perdue')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('réponse de pause illisible', p.stderr)
        self.assertIn("drain posé (confirmé par l'état réel)", p.stdout)
        self.assertIn('drain levé (redémarrage hors tour)', p.stdout)
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_cut_pause_answer_after_commit_is_reconciled(self):
        """R1 B2 : la pose aboutit puis le lien tombe (rc ≠ 0) ; même
        réconciliation, la pause trouvée est bien la nôtre."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='pause-coupee')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("drain posé (confirmé par l'état réel)", p.stdout)
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_lost_pause_answer_without_our_pause_stops_safely(self):
        """R1 B2 : réponse illisible et aucune pause à nous ; rien n'est levé
        ni redémarré, et le script s'arrête en le disant."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='pause-perdue-sans-pose')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("n'est pas celle de ce drain", p.stderr)
        self.assertFalse((self.state / 'pause-dev9').exists())
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])

    def test_lost_pause_and_reconciliation_still_attempt_conditional_cleanup(self):
        """A committed pause stays tracked when its reconciliation read fails."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='pause-coupee-etat-perdu')
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue((self.state / 'lecture-perdue').exists())
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])
        self.assertFalse((self.state / 'pause-dev9').exists())
        self.assertIn('resume dev9 --operator', self.operateur())
        self.assertIn('--expect-reason', self.operateur())

    def test_operator_gesture_refused_in_an_agent_session_stops_safely(self):
        """Périmètre annoncé : le drain est un geste d'opérateur. Un refus
        bad_actor (vm.sh lancé dans une session liée à un agent) n'interrompt
        aucun tour et ne laisse aucune pause."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='drain-refuse-identite')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bad_actor', p.stderr)
        self.assertIn('pose du drain refusée', p.stderr)
        self.assertIn("n'est pas celle de ce drain", p.stderr)
        self.assertFalse((self.state / 'pause-dev9').exists())
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])

    def test_drain_refused_stops_without_restarting_the_agent(self):
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines-pause-error')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('pose du drain refusée', p.stderr)
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])
        self.assertNotIn('resume dev9', self.operateur())
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_drain_lifts_the_pause_when_the_timeout_is_reached(self):
        """N4 : le drain ne survit pas à l'échéance ; celle-ci vient de
        l'horloge injectée (aucun budget de temps réel)."""
        self.env['DRAIN_ATTENTE'] = '1'
        self.env['ATTENTE_MAX'] = '5'
        (self.state / 'horloge').write_text('0')
        self.env['HORLOGE_FICHIER'] = str(self.state / 'horloge')
        p = self.run_vm(scenario='chaines-toujours')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('toujours en tour', p.stderr)
        self.assertIn('pause dev9', self.operateur())
        self.assertIn('resume dev9', self.operateur())
        self.assertIn('drain levé (sortie du script)', p.stdout)
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_second_drain_after_a_human_lift_tracks_the_live_pause(self):
        """N5 B2 : après une levée humaine, le re-drain ne garde pas le motif
        périmé ; c'est la pause vivante qui est levée à la sortie."""
        self.env['DRAIN_ATTENTE'] = '1'
        self.env['ATTENTE_MAX'] = '10'
        (self.state / 'horloge').write_text('0')
        self.env['HORLOGE_FICHIER'] = str(self.state / 'horloge')
        p = self.run_vm(scenario='chaines-relance')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('toujours en tour', p.stderr)
        self.assertTrue((self.state / 'levee-humaine').exists())
        poses = [l for l in self.operateur().splitlines() if l.startswith('pause dev9')]
        self.assertEqual(len(poses), 2)
        self.assertIn('drain levé (sortie du script)', p.stdout)
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_resume_failure_is_named_and_the_hold_stays_visible(self):
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines-resume-error')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('drain NON levé', p.stderr)
        self.assertIn('ameesh resume dev9 --operator', p.stderr)
        self.assertTrue((self.state / 'pause-dev9').exists())

    def test_foreign_pause_is_neither_doubled_nor_lifted(self):
        self.env['DRAIN_ATTENTE'] = '1'
        (self.state / 'pause-dev9').touch()
        p = self.run_vm(scenario='chaines')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('pause dev9', self.operateur())
        self.assertNotIn('resume dev9', self.operateur())
        self.assertTrue((self.state / 'pause-dev9').exists())
        self.assertIn('restart ameesh-runner-agent@dev9.service', self.units())

    def test_concurrent_pause_is_left_in_place(self):
        """Posée entre notre lecture et notre pause : elle n'est pas de nous."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='pause-concurrente')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('pause dev9', self.operateur())
        self.assertNotIn('resume dev9', self.operateur())
        self.assertIn('déjà posée par un autre acteur', p.stdout)
        self.assertTrue((self.state / 'pause-dev9').exists())

    def test_drain_disabled_by_zero(self):
        self.env['DRAIN_ATTENTE'] = '0'
        self.env['ATTENTE_MAX'] = '0'
        p = self.run_vm(scenario='chaines-toujours')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('toujours en tour', p.stderr)
        self.assertEqual(self.operateur(), '')

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

    def test_check_visits_every_registered_agent(self):
        """#1455: the client must not eat the list shared with the `while read`."""
        p = self.run_vm('controler')
        self.assertEqual(p.returncode, 0, p.stderr)
        seen = self.units()
        for name in ('dev8', 'dev9'):
            self.assertIn(f'ameesh-runner-agent@{name}.service', seen)
        self.assertIn('dev8', p.stdout)
        self.assertIn('dev9', p.stdout)

    def test_check_fails_when_only_the_last_agent_is_broken(self):
        """#1455: a defect on the last listed agent alone must red the check."""
        p = self.run_vm('controler', scenario='inactive-dev9')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('dev9', p.stdout)
        self.assertIn('contrôles en échec', p.stderr)

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
        # Le délai global tombe avant DRAIN_ATTENTE (300 s) : aucun drain.
        self.assertEqual(self.operateur(), '')

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
        p = self.run_vm('publier')
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
