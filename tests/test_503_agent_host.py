# SPDX-License-Identifier: AGPL-3.0-only
"""Agent-host deployment through fake SSH: no VM, unit or production database."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import shutil
import sys
import tempfile
import time
import unittest

from tests import support  # isolate the workstation environment

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/mise-a-jour/vm.sh'

SSH = r'''#!/usr/bin/env python3
import os, signal, subprocess, sys
from pathlib import Path
if os.environ.get('FAKE_STALL_SSH') \
        and os.environ.get('FAKE_STALL_MATCH', '') in sys.argv[-1]:
    # #2198/N1 : lien vivant et muet ; le signal reçu par le client doit être
    # transmis à l'enfant (le vrai ssh). On le trace pour le banc.
    def _recu(signum, frame):
        if os.environ.get('FAKE_SIGNAL_FILE'):
            Path(os.environ['FAKE_SIGNAL_FILE']).write_text(str(signum))
        sys.exit(128 + signum)
    signal.signal(signal.SIGINT, _recu)
    signal.signal(signal.SIGTERM, _recu)
    # Le témoin n'est posé qu'une fois les gestionnaires installés : le banc
    # peut alors envoyer le signal sans course.
    if os.environ.get('FAKE_STALL_STARTED'):
        Path(os.environ['FAKE_STALL_STARTED']).write_text('1')
    signal.pause()
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
import json, os, subprocess, sys, time
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
if sys.argv[1:] == ['pause', '--help']:
    # #2184/R3 : sans --not-after, la pose n'a pas de barrière ; le script
    # désactive le drain plutôt que de poser sans échéance.
    if (state / 'sans-not-after').exists():
        print('usage: ameesh pause AGENT --reason R [--human H] [--json]')
    else:
        print('usage: ameesh pause AGENT --reason R [--human H] '
              '[--not-after EPOCH] [--json]')
    sys.exit(0)
if sys.argv[1:] == ['list', '--json']:
    if scenario == 'list-error': sys.exit(1)
    if scenario == 'bad-json': print('not json'); sys.exit(0)
    print((state / 'agents.json').read_text())
elif sys.argv[1] == 'show':
    name = sys.argv[2]
    # #2198/N4 : lecture lente ; la borne de lecture doit la couper avant la
    # borne (plus longue) des gestes de drain.
    if scenario == 'show-lent':
        time.sleep(5)
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
    # #2184/N1 : le mode peut changer entre la liste et la relecture ; dev9
    # était execute au listage et passe externe au show.
    if scenario == 'mode-change' and name == 'dev9': row['mode'] = 'externe'
    # #2184/N1 + 0459 : le mode change alors qu'un drain est déjà posé ; la
    # relecture doit encore pouvoir constater et lever notre pause.
    if scenario == 'mode-change-apres-drain' and name == 'dev9' and pause.exists():
        row['mode'] = 'externe'
    if scenario == 'always-busy': row['status'] = 'running'
    # #2152 : un agent qui enchaîne les tours ne rend la main qu'une fois
    # drainé ; « chaines-toujours » ne la rend jamais.
    if name == 'dev9' and scenario in ('chaines', 'chaines-pause-error',
                                       'chaines-resume-error', 'pause-concurrente',
                                       'drain-remplace', 'pause-reponse-perdue',
                                       'pause-coupee', 'pause-coupee-etat-perdu',
                                       'pause-perdue-sans-pose', 'drain-puis-stop-error',
                                       'drain-refuse-identite', 'pause-expiree-tardive',
                                       'mode-change-apres-drain', 'resume-error-deux-fois') \
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
    # #2184/R3 : un créateur tardif indépendant (systemd-run retardé) rejoue
    # la pose APRÈS la relecture. La base doit la refuser au-delà de
    # --not-after ; sinon la pause resterait orpheline.
    if scenario == 'pause-expiree-tardive' and (state / 'resume-seen').exists() \
            and (state / ('motif-tardif-' + name)).exists() \
            and not (state / 'createur-tardif-fait').exists():
        (state / 'createur-tardif-fait').touch()
        echeance = (state / ('echeance-' + name)).read_text().strip()
        subprocess.run(
            [sys.argv[0], 'pause', name,
             '--reason', (state / ('motif-tardif-' + name)).read_text(),
             '--not-after', echeance, '--json'],
            env=dict(os.environ), capture_output=True, text=True)
elif sys.argv[1] in ('pause', 'resume'):
    # #2152 : les gestes du drain, avec le « changed » que lit le script.
    name = sys.argv[2]
    args = sys.argv[3:]
    pause = state / ('pause-' + name)
    motif = state / ('motif-' + name)
    with (state / 'operateur.log').open('a') as f:
        f.write('%s %s %s\n' % (sys.argv[1], name, ' '.join(args)))
    if '--reason' not in args: sys.exit(2)
    if sys.argv[1] == 'resume':
        (state / 'resume-seen').touch()
    if sys.argv[1] == 'pause':
        # #2184/R3 : la base refuse une pose au-delà de --not-after. Le banc
        # compare à l'horloge injectée (HORLOGE_FICHIER) quand elle est posée.
        echeance = None
        if '--not-after' in args:
            echeance = float(args[args.index('--not-after') + 1])
        hf = os.environ.get('HORLOGE_FICHIER')
        maintenant = float(Path(hf).read_text()) if hf and Path(hf).exists() else time.time()
        if echeance is not None and maintenant >= echeance:
            with (state / 'refus-tardif.log').open('a') as f:
                f.write('%s not_after\n' % name)
            print(json.dumps({'agent': name, 'mode': 'execute',
                              'operator_paused': pause.exists(),
                              'changed': False, 'event_id': None,
                              'refusal': 'not_after'}))
            sys.exit(0)
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
        if scenario == 'pause-expiree-tardive':
            # Timeout de l'appel (rc 124) : la pose n'est pas encore commitée.
            # Le créateur tardif rejouera cette commande avec la même échéance.
            (state / ('motif-tardif-' + name)).write_text(
                args[args.index('--reason') + 1])
            if echeance is not None:
                (state / ('echeance-' + name)).write_text(str(echeance))
            sys.exit(124)
        motif.write_text(args[args.index('--reason') + 1])
        pause.touch()
        with (state / 'sequence.log').open('a') as f:
            f.write('pause %s\n' % name)
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
        if scenario == 'resume-error-deux-fois':
            # #2198/N3 : les deux premières levées échouent (celle de la boucle
            # puis la première passe du trap) ; la seconde passe réussit.
            compteur = state / 'resume-compte'
            compte = int(compteur.read_text()) if compteur.exists() else 0
            compteur.write_text(str(compte + 1))
            if compte < 2:
                sys.exit(1)
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
name = unit.split('@')[1].removesuffix('.service')
with (state / 'units.log').open('a') as f: f.write(' '.join(args) + '\n')
scenario = (state / 'scenario').read_text()
def stopped():
    return (state / ('stopped-' + name)).exists()
if 'stop' in args:
    with (state / 'sequence.log').open('a') as f:
        f.write('stop %s\n' % name)
    if scenario == 'stop-error' or (scenario == 'drain-puis-stop-error' and name == 'dev9'):
        sys.exit(1)
    if scenario != 'stop-unproven': (state / ('stopped-' + name)).touch()
    if scenario == 'stop-failed': (state / ('failed-' + name)).touch()
elif 'reset-failed' in args:
    if scenario == 'reset-error': sys.exit(1)
    if scenario != 'reset-no-effect':
        (state / ('failed-' + name)).unlink(missing_ok=True)
elif 'start' in args:
    if scenario == 'restart-error': sys.exit(1)
    (state / ('restart-' + name)).touch()
    (state / ('stopped-' + name)).unlink(missing_ok=True)
elif 'restart' in args:
    # old path, forbidden by #1152: the fakes below keep it visible
    if scenario == 'restart-error': sys.exit(1)
    (state / ('restart-' + name)).touch()
elif 'show' in args:
    if 'ActiveState' in args:
        if (state / ('failed-' + name)).exists(): print('failed')
        else: print('inactive' if stopped() else 'active')
    elif 'MainPID' in args:
        print('0' if stopped() else '123')
    elif scenario == 'bad-execstart': print('unreadable')
    elif scenario == 'execstart-error': sys.exit(1)
    else:
        owner = 'runner-' + name + '@host-agents'
        option = '--runner-id ' + owner
        if scenario == 'equals-owner': option = '--runner-id=' + owner
        if scenario == 'abbreviated-owner': option = '--r=' + owner
        if scenario == 'last-owner': option = '--runner-id wrong --runner-id=' + owner
        if scenario == 'default-owner': option = ''
        print('{ path=/bin/runner ; argv[]=/bin/runner run ' + option + ' ; }')
elif 'is-active' in args:
    if scenario == 'inactive' or stopped(): sys.exit(3)
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

#: #2152 : plus aucune attente de créneau « idle » (DRAIN_ATTENTE est un
#: interrupteur) ; les attentes de bail (5 s) restent instantanées. #2184/R3 :
#: l'attente de l'échéance d'une pose incertaine avance l'horloge du banc.
SLEEP = ('#!/bin/sh\n'
         'if [ "$1" = 15 ]; then\n'
         '  if [ -n "${HORLOGE_FICHIER:-}" ]; then\n'
         '    echo "$(( $(cat "$HORLOGE_FICHIER") + 2 ))" > "$HORLOGE_FICHIER"\n'
         '  fi\n'
         '  exec %s 0.5\n'
         'fi\n'
         'exit 0\n') % (shutil.which('sleep') or '/bin/sleep')

#: #2184/R3 : l'échéance absolue remise à la base suit l'horloge injectée,
#: comme le faux CLI qui la compare. Sans fichier d'horloge, date réelle.
DATE = ('#!/bin/sh\n'
        'if [ -n "${HORLOGE_FICHIER:-}" ] && [ -r "$HORLOGE_FICHIER" ] '
        '&& [ "$1" = +%%s ]; then\n'
        '  cat "$HORLOGE_FICHIER"\n'
        '  exit 0\n'
        'fi\n'
        'exec %s "$@"\n') % (shutil.which('date') or '/bin/date')


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
                             ('date', DATE), ('id', '#!/bin/sh\necho 1001\n'),
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

    def starts(self):
        """Démarrages (`start`) d'unités observés ; l'ancien `restart` ne doit
        plus apparaître (#1152)."""
        return [l for l in self.units().splitlines()
                if l.startswith('--user start ameesh-runner-agent@')]

    def stops(self):
        return [l for l in self.units().splitlines()
                if l.startswith('--user stop ameesh-runner-agent@')]

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
        self.assertEqual(self.starts(), ['--user start ameesh-runner-agent@dev8.service',
                                         '--user start ameesh-runner-agent@dev9.service'])
        # #1152 : les personas actives prouvent leur arrêt puis redémarrent,
        # jamais par `restart`. #1633 : la persona arrêtée du registre est du
        # lot — son unité active est seulement rechargée sur le nouveau code
        # (aucun bail exigé) ; l'observateur et l'agent d'un autre hôte restent
        # ignorés.
        restarts = [l for l in self.units().splitlines() if 'restart' in l]
        self.assertEqual(restarts, ['--user restart ameesh-runner-agent@stopped.service'])
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

    def test_restart_stops_proves_then_starts(self):
        """#1152 : l'arrêt (SIGTERM, drainage) précède la preuve du point sûr
        (ActiveState=inactive ET MainPID=0), elle-même avant le démarrage ;
        `restart`, qui ne prouve rien, n'est jamais utilisé."""
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        log = self.units().splitlines()
        for name in ('dev8', 'dev9'):
            unite = 'ameesh-runner-agent@' + name + '.service'
            stop = log.index('--user stop ' + unite)
            before = log.index('--user show -p ActiveState --value ' + unite)
            actif = log.index('--user show -p ActiveState --value ' + unite, stop + 1)
            pid = log.index('--user show -p MainPID --value ' + unite)
            start = log.index('--user start ' + unite)
            self.assertLess(before, stop, unite)
            self.assertLess(stop, actif, unite)
            self.assertLess(actif, start, unite)
            self.assertLess(pid, start, unite)
        # `restart` ne prouve rien pour une persona active (#1152) ; seule la
        # persona arrêtée du registre recharge son unité par `restart` (#1633).
        for unite in ('ameesh-runner-agent@dev8.service',
                      'ameesh-runner-agent@dev9.service'):
            self.assertNotIn('--user restart ' + unite, self.units())

    def test_preexisting_failed_units_are_reset_before_proven_stop(self):
        for name in ('dev8', 'dev9'):
            (self.state / ('failed-' + name)).touch()
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        log = self.units().splitlines()
        for name in ('dev8', 'dev9'):
            unit = 'ameesh-runner-agent@' + name + '.service'
            before = log.index('--user show -p ActiveState --value ' + unit)
            reset = log.index('--user reset-failed ' + unit)
            stop = log.index('--user stop ' + unit)
            proof = log.index('--user show -p ActiveState --value ' + unit, stop + 1)
            pid = log.index('--user show -p MainPID --value ' + unit, stop + 1)
            start = log.index('--user start ' + unit)
            self.assertLess(before, reset)
            self.assertLess(reset, stop)
            self.assertLess(stop, proof)
            self.assertLess(proof, start)
            self.assertLess(pid, start)
            self.assertIn(name + ' : redémarré', p.stdout)

    def test_preexisting_failure_does_not_bypass_stop_proof(self):
        unit = 'ameesh-runner-agent@dev8.service'
        for scenario in ('stop-failed', 'reset-error', 'reset-no-effect'):
            with self.subTest(scenario=scenario):
                (self.state / 'units.log').unlink(missing_ok=True)
                (self.state / 'failed-dev8').touch()
                p = self.run_vm(scenario=scenario)
                self.assertNotEqual(p.returncode, 0, p.stdout)
                log = self.units().splitlines()
                reset = log.index('--user reset-failed ' + unit)
                self.assertEqual(self.starts(), [])
                self.assertNotIn('--user stop ameesh-runner-agent@dev9.service', log)
                if scenario == 'reset-error':
                    self.assertNotIn('--user stop ' + unit, log)
                else:
                    self.assertLess(reset, log.index('--user stop ' + unit))

    def test_restart_gates_agent_in_turn_without_idle_wait(self):
        # #1152/R1 : un agent vu `running` n'est plus différé — il est arrêté
        # (SIGTERM, drainage borné), prouvé au point sûr, puis démarré.
        p = self.run_vm(scenario='busy-once')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((self.state / 'busy-seen').exists())
        self.assertIn('--user stop ameesh-runner-agent@dev9.service', self.units())
        self.assertIn('--user start ameesh-runner-agent@dev9.service', self.units())
        self.assertNotIn('--user restart ameesh-runner-agent@dev9.service', self.units())

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
        motif = pose.split('--reason ', 1)[1].split(' --human ', 1)[0]
        self.assertIn('--expect-reason ' + motif, reprise)
        # Posé avant le redémarrage, et redémarré seulement hors tour.
        self.assertLess(p.stdout.index('drain posé'), p.stdout.index('dev9 : redémarré'))
        self.assertIn('dev9 : redémarré (était idle)', p.stdout)
        self.assertIn('drain levé (redémarrage hors tour)', p.stdout)
        self.assertIn('reprise prouvée (operator_paused=false)', p.stdout)
        self.assertFalse((self.state / 'pause-dev9').exists())

    def _un_seul_agent_au_tour(self):
        """Un banc direct (Popen) : un seul agent, vu en tour, pour que la
        première lecture réussisse et que ce soit bien la pose qui bloque."""
        (self.state / 'scenario').write_text('chaines')
        (self.state / 'agents.json').write_text(
            json.dumps([self.agents[0]]))
        # Une pose incertaine fait attendre l'échéance au trap EXIT ; l'horloge
        # injectée (le faux `sleep 15` avance le fichier) rend l'attente nulle.
        (self.state / 'horloge').write_text('0')
        self.env.update(DRAIN_ATTENTE='1', FAKE_STALL_SSH='1',
                        FAKE_STALL_MATCH='ameesh pause dev9',
                        DRAIN_ECHEANCE='1', DRAIN_BORNE_INSTRUCTION='1',
                        DRAIN_DERIVE='1',
                        HORLOGE_FICHIER=str(self.state / 'horloge'))

    def test_a_live_ssh_client_is_bounded_by_the_drain_deadline(self):
        """#2184/N3 : un client SSH vivant mais bloqué ne suspend pas le
        balayage au-delà de DRAIN_APPEL_DELAI (borne locale du groupe)."""
        self._un_seul_agent_au_tour()
        self.env.update(DRAIN_APPEL_DELAI='1', DRAIN_MARGE_LOCALE='1')
        proc = subprocess.Popen(['bash', str(SCRIPT), 'agents', 'redemarrer'],
                                env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            _, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            proc.communicate()
            self.fail('drain call outlived its deadline with a live SSH client')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('appel de drain expiré', stderr)
        self.assertEqual(self.units(), '')

    def test_the_local_deadline_leaves_a_margin_to_the_remote_one(self):
        """#2198/N2 : la borne locale vaut DRAIN_APPEL_DELAI plus une marge,
        pour que le timeout distant rende son code avant que le client ne tue
        le groupe. Sans marge, les deux expiraient ensemble."""
        self._un_seul_agent_au_tour()
        self.env.update(DRAIN_APPEL_DELAI='1', DRAIN_MARGE_LOCALE='2')
        debut = time.monotonic()
        proc = subprocess.Popen(['bash', str(SCRIPT), 'agents', 'redemarrer'],
                                env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            _, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            proc.communicate()
            self.fail('drain call outlived its deadline with a live SSH client')
        ecoule = time.monotonic() - debut
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('appel de drain expiré', stderr)
        self.assertGreaterEqual(ecoule, 2.5,
                                'la borne locale doit inclure DRAIN_MARGE_LOCALE')

    def test_interruption_is_forwarded_to_the_stalled_ssh_child(self):
        """#2198/N1 : le client lance son enfant dans une nouvelle session ;
        un SIGINT reçu par le client doit être transmis à cet enfant, sinon
        le ssh reste orphelin."""
        self._un_seul_agent_au_tour()
        self.env.update(DRAIN_APPEL_DELAI='30',
                        FAKE_STALL_STARTED=str(self.state / 'ssh-commence'),
                        FAKE_SIGNAL_FILE=str(self.state / 'signal'))
        proc = subprocess.Popen(['bash', str(SCRIPT), 'agents', 'redemarrer'],
                                env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            limite = time.monotonic() + 10
            while not (self.state / 'ssh-commence').exists() \
                    and time.monotonic() < limite:
                time.sleep(0.05)
            self.assertTrue((self.state / 'ssh-commence').exists())
            os.killpg(proc.pid, signal.SIGINT)
            _, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            proc.communicate()
            self.fail('interrupted drain call did not end')
        self.assertTrue((self.state / 'signal').exists(), stderr)
        self.assertEqual((self.state / 'signal').read_text(), str(signal.SIGINT))
        self.assertNotEqual(proc.returncode, 0)

    def test_reads_are_bounded_by_their_own_deadline(self):
        """#2198/N4 : list/show/--help ont une borne plus courte que celle des
        gestes de drain ; une lecture lente ne bloque pas le balayage."""
        self.env.update(DRAIN_APPEL_DELAI='10', DRAIN_MARGE_LOCALE='1',
                        DRAIN_LECTURE_DELAI='1')
        debut = time.monotonic()
        p = self.run_vm(scenario='show-lent')
        ecoule = time.monotonic() - debut
        self.assertNotEqual(p.returncode, 0)
        self.assertLess(ecoule, 5,
                        'show doit être coupé par DRAIN_LECTURE_DELAI, pas par '
                        'DRAIN_APPEL_DELAI')

    def test_failed_lift_is_not_announced_before_the_exit_retry(self):
        """#2198/N3 : « NON levé » n'apparaît qu'après la dernière tentative
        du trap EXIT ; une levée qui réussit à la seconde passe ne l'affiche
        pas et ne laisse pas de pause."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='resume-error-deux-fois')
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn('drain NON levé', p.stderr)
        self.assertNotIn('NON levé', p.stderr)
        self.assertNotIn('pause à lever à la main', p.stderr)
        self.assertFalse((self.state / 'pause-dev9').exists())
        reprises = [l for l in self.operateur().splitlines()
                    if l.startswith('resume dev9')]
        self.assertEqual(len(reprises), 3,
                         'une levée dans la boucle, deux passes du trap EXIT')

    def test_drain_carries_a_deadline_and_the_operator_actor(self):
        """#2184/R3 et N6 : la pose porte une échéance absolue et l'acteur
        humain du poste, jamais le compte `ameesh` de la VM."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines')
        self.assertEqual(p.returncode, 0, p.stderr)
        pose = [l for l in self.operateur().splitlines() if l.startswith('pause dev9')][0]
        self.assertIn('--not-after ', pose)
        self.assertIn('--human human:', pose)

    def test_cli_without_not_after_never_poses_a_drain(self):
        """#2184/R3 : un CLI cible sans --not-after ne peut pas porter la
        barrière ; le drain est désactivé plutôt que posé sans échéance."""
        self.env['DRAIN_ATTENTE'] = '1'
        (self.state / 'sans-not-after').touch()
        p = self.run_vm(scenario='chaines')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('drain #2152 indisponible', p.stderr)
        self.assertIn('--not-after', p.stderr)
        self.assertNotIn('pause dev9', self.operateur())
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_timed_out_pause_cannot_commit_after_the_deadline(self):
        """#2184/R3 : un appel pause expiré (rc 124) peut encore créer son
        unité après la dernière lecture. Le créateur tardif rejoue la pose
        APRÈS la reprise : la barrière d'échéance côté base la refuse, sans
        événement ni pause orpheline."""
        self.env['DRAIN_ATTENTE'] = '1'
        self.env['DRAIN_ECHEANCE'] = '1'
        self.env['DRAIN_BORNE_INSTRUCTION'] = '1'
        self.env['DRAIN_DERIVE'] = '1'
        (self.state / 'horloge').write_text('0')
        self.env['HORLOGE_FICHIER'] = str(self.state / 'horloge')
        p = self.run_vm(scenario='pause-expiree-tardive')
        self.assertNotEqual(p.returncode, 0)
        # Le créateur tardif a bien exécuté sa pose après le nettoyage…
        self.assertTrue((self.state / 'createur-tardif-fait').exists())
        # … et la base l'a refusée : aucune pause, aucun événement.
        self.assertEqual((self.state / 'refus-tardif.log').read_text().strip(),
                         'dev9 not_after')
        self.assertFalse((self.state / 'pause-dev9').exists())
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])
        self.assertIn('resume dev9 --operator', self.operateur())
        self.assertIn('--expect-reason', self.operateur())

    def test_drain_refused_does_not_stop_the_other_agents(self):
        """#2184/N2 : un refus de pose ne condamne que cet agent. Les autres
        agents du balayage sont redémarrés ; l'agent non drainé n'est ni
        redémarré ni repris."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines-pause-error')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('pose du drain refusée', p.stderr)
        self.assertIn('--user start ameesh-runner-agent@dev8.service', self.units())
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])
        self.assertNotIn('resume dev9', self.operateur())
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_non_execute_agent_is_skipped_before_restart(self):
        """#2184/N1 : un agent passé en mode non-execute pendant le drain
        n'est ni drainé ni redémarré ; le motif du skip le nomme."""
        p = self.run_vm(scenario='mode-change')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('dev9 : déplacé ou mode non execute (mode=externe), ignoré',
                      p.stdout)
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])
        self.assertEqual(self.operateur(), '')

    def test_mode_change_after_a_drain_still_lifts_the_pause(self):
        """#2184/N1 + 0459 : l'agent passe externe après la pose du drain ;
        la pause de ce drain est levée et l'agent n'est pas redémarré."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='mode-change-apres-drain')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('dev9 : déplacé ou mode non execute (mode=externe), ignoré',
                      p.stdout)
        self.assertIn('dev9 : drain levé', p.stdout)
        self.assertFalse((self.state / 'pause-dev9').exists())
        self.assertEqual([l for l in self.units().splitlines() if '@dev9.' in l], [])
        self.assertIn('resume dev9 --operator', self.operateur())

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

    def test_drain_does_not_wait_out_a_turn_that_never_ends(self):
        """#2152 : le drain ne bloque plus l'arrêt. Un agent qui reste en tour
        reçoit la pause, puis l'arrêt prouvé #1152 prend la suite sans attendre
        une échéance ; la pause est levée après le démarrage."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines-toujours')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('toujours en tour', p.stderr)
        self.assertIn('pause dev9', self.operateur())
        self.assertIn('resume dev9', self.operateur())
        self.assertIn('--user stop ameesh-runner-agent@dev9.service', self.units())
        self.assertIn('--user start ameesh-runner-agent@dev9.service', self.units())
        self.assertIn('drain levé (redémarrage hors tour)', p.stdout)
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_pause_is_posed_before_the_stop(self):
        """#2152 x #1152 : la pause ferme l'admission AVANT le SIGTERM."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines-toujours')
        self.assertEqual(p.returncode, 0, p.stderr)
        seq = (self.state / 'sequence.log').read_text().splitlines()
        self.assertLess(seq.index('pause dev9'), seq.index('stop dev9'))

    def test_drain_is_lifted_when_the_proven_stop_fails(self):
        """Pose réussie puis `stop` en échec : le trap EXIT lève NOTRE pause."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='drain-puis-stop-error')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('drain posé', p.stdout)
        self.assertIn('ne redémarre pas', p.stderr)
        self.assertIn('resume dev9 --operator', self.operateur())
        self.assertIn('--expect-reason', self.operateur())
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_human_lift_before_the_resume_leaves_no_stale_drain(self):
        """N5 B2 : notre drain est levé par un humain après la pose ; la levée
        conditionnelle ne retrouve plus la nôtre et ne pose rien de périmé."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm(scenario='chaines-relance')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('toujours en tour', p.stderr)
        self.assertTrue((self.state / 'levee-humaine').exists())
        poses = [l for l in self.operateur().splitlines() if l.startswith('pause dev9')]
        self.assertEqual(len(poses), 1)
        self.assertIn('stop ameesh-runner-agent@dev9.service', self.units())
        self.assertIn('start ameesh-runner-agent@dev9.service', self.units())
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
        # La pause étrangère n'empêche pas l'arrêt prouvé #1152.
        self.assertIn('--user stop ameesh-runner-agent@dev9.service', self.units())
        self.assertIn('--user start ameesh-runner-agent@dev9.service', self.units())
        self.assertNotIn('--user restart ameesh-runner-agent@dev9.service', self.units())

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
        p = self.run_vm(scenario='chaines-toujours')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('toujours en tour', p.stderr)
        self.assertEqual(self.operateur(), '')
        self.assertIn('--user stop ameesh-runner-agent@dev9.service', self.units())
        self.assertIn('--user start ameesh-runner-agent@dev9.service', self.units())

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
        self.assertIn('start ameesh-runner-agent@dev8.service', self.units())

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
                self.assertNotIn(' stop ', self.units())
                self.assertNotIn(' start ', self.units())

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
        self.assertNotIn('start', self.units())

    def test_check_rejects_expired_lease(self):
        p = self.run_vm('controler', 'expired')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bail', p.stderr)

    def test_check_accepts_held_lease_and_is_read_only(self):
        p = self.run_vm('controler')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('contrôles verts', p.stdout)
        self.assertNotIn('restart', self.units())
        self.assertNotIn('start', self.units())

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
        for scenario in ['stop-error', 'stop-unproven', 'stop-failed',
                         'restart-error', 'inactive']:
            with self.subTest(scenario=scenario):
                p = self.run_vm(scenario=scenario)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn('ameesh-runner-agent@dev8.service', p.stderr)
                self.assertNotIn('redémarré', p.stdout)

    def test_moved_agent_is_skipped_before_restart(self):
        p = self.run_vm(scenario='moved-before')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.units(), '')
        self.assertIn('dev9 : déplacé ou mode non execute (hôte=elsewhere), ignoré', p.stdout)

    def test_host_change_after_restart_is_not_a_valid_proof(self):
        p = self.run_vm(scenario='moved')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('bail', p.stderr)

    def test_long_turn_is_drained_not_waited_out(self):
        # #1152/R1 (starvation impl07) : `running` à chaque lecture ne fait
        # plus expirer la mise à jour — chaque unité est arrêtée (drainage
        # borné par TimeoutStopSec), prouvée, puis démarrée.
        p = self.run_vm(scenario='always-busy')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('toujours en tour', p.stderr)
        for name in ('dev8', 'dev9'):
            self.assertIn('--user stop ameesh-runner-agent@%s.service' % name,
                          self.units())
            self.assertIn('--user start ameesh-runner-agent@%s.service' % name,
                          self.units())
        self.assertNotIn('restart', self.units())
        # DRAIN_ATTENTE vaut 0 par défaut (#1152) : aucun drain n'est posé.
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
            self.assertIn('stop ameesh-runner-agent@' + name + '.service', self.units())
            self.assertIn('start ameesh-runner-agent@' + name + '.service', self.units())

    def test_invalid_name_is_named_and_skipped_without_blocking_valid_agents(self):
        bad = dict(name='bad;touch injected', host='host-agents', mode='execute', status='idle')
        p = self.run_vm(agents=self.agents + [bad])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("agent 'bad;touch injected' : nom invalide", p.stderr)
        self.assertIn('start ameesh-runner-agent@dev9.service', self.units())
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
