# SPDX-License-Identifier: AGPL-3.0-only
"""#2152 — `vm.sh redemarrer` (mode serveur) : un agent qui enchaîne les tours
est drainé par une pause opérateur courte, puis la pause est levée dès qu'il
est redémarré hors tour.

Faux `ssh` (aucune VM : lectures de migrations, unité) et faux CLI
`ameesh` du poste (registre, pause, reprise) sur PATH. Les règles du drain
(posé/levé/non levé, trap de sortie) sont couvertes en mode agents par
`tests/test_503_agent_host.py` ; ce banc prouve le câblage du mode serveur,
où la pause passe par le CLI du poste.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/mise-a-jour/vm.sh"

FAKE_SSH = r'''#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
cmd = sys.argv[-1]
with (state / 'ssh.log').open('a') as f:
    f.write(cmd.replace('\n', ' ') + '\n')
if 'ls-tree --name-only' in cmd:
    print('src/ameesh/migrations/0001_init.sql')
elif 'schema_migrations' in cmd:
    print('1')
elif 'clock_timestamp' in cmd:
    print(time.time())
elif 'systemctl show' in cmd and 'ActiveState' in cmd:
    print('inactive' if (state / 'stopped-dev9').exists() else 'active')
elif 'systemctl show' in cmd and 'MainPID' in cmd:
    print('0' if (state / 'stopped-dev9').exists() else '123')
elif 'systemctl show' in cmd:
    print('{ path=/bin/runner ; argv[]=/bin/runner run --runner-id runner-dev9@mesh ; }')
elif 'systemctl stop' in cmd:
    (state / 'stopped-dev9').touch()
elif 'systemctl start' in cmd:
    (state / 'restart-dev9').touch()
    (state / 'stopped-dev9').unlink(missing_ok=True)
elif 'systemctl restart' in cmd:
    (state / 'restart-dev9').touch()
elif 'rev-parse' in cmd:
    print('0' * 40)
sys.exit(0)
'''

FAKE_AMEESH = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
with (state / 'poste.log').open('a') as f:
    f.write(' '.join(sys.argv[1:]) + '\n')
action = sys.argv[1] if len(sys.argv) > 1 else ''
name = sys.argv[2] if len(sys.argv) > 2 else ''
pause = state / ('pause-' + name)
motif = state / ('motif-' + name)


def opt(flag):
    args = sys.argv[3:]
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None


if action == 'resume' and sys.argv[2:] == ['--help']:
    # #2152 : le script ne draine que si la reprise conditionnelle existe.
    # Le drapeau simule un CLI antérieur (N5 B1).
    if (state / 'sans-expect-reason').exists():
        print('usage: ameesh resume ... --operator --reason R')
    else:
        print('usage: ameesh resume ... --operator --reason R --expect-reason TEXT')
    sys.exit(0)
if action == 'pause' and sys.argv[2:] == ['--help']:
    # #2184/R3 : sans --not-after, la pose ne porte pas de barrière ; le
    # script doit refuser de drainer. Le drapeau simule un CLI antérieur.
    if (state / 'sans-not-after').exists():
        print('usage: ameesh pause AGENT --reason R [--human H] [--json]')
    else:
        print('usage: ameesh pause AGENT --reason R [--human H] '
              '[--not-after EPOCH] [--json]')
    sys.exit(0)
if action == 'show':
    paused = pause.exists()
    # #2152 : l'agent enchaîne les tours tant qu'il n'est pas drainé ; drainé,
    # il finit son tour et devient idle. Après redémarrage, le bail est repris
    # (epoch 9, comme le veut bail.sh).
    etat = 'idle' if paused else 'running'
    # #2198/N5 : le mode de la persona, honoré aussi en mode serveur.
    mode = 'externe' if (state / 'mode-externe').exists() else 'execute'
    print(json.dumps({'name': name, 'host': 'mesh', 'mode': mode, 'status': etat,
                      'lease_owner': 'runner-dev9@mesh',
                      'lease_epoch': 9 if (state / 'restart-dev9').exists() else 7,
                      'lease_expires_ts': time.time() + 300,
                      'operator_paused': paused,
                      'operator_pause_reason': (motif.read_text() if motif.exists() else None)}))
elif action == 'pause':
    motif.write_text(opt('--reason') or '')
    pause.touch()
    print(json.dumps({'agent': name, 'mode': 'execute', 'operator_paused': True,
                      'changed': True, 'event_id': 1}))
elif action == 'resume':
    if (state / 'sans-expect-reason').exists() and '--expect-reason' in sys.argv[3:]:
        sys.exit(2)
    attendu = opt('--expect-reason')
    raison = motif.read_text() if motif.exists() else ''
    if pause.exists() and attendu is not None and raison == attendu:
        pause.unlink()
        motif.unlink(missing_ok=True)
        print(json.dumps({'agent': name, 'mode': 'execute', 'operator_paused': False,
                          'changed': True, 'event_id': 2}))
    else:
        # Reprise conditionnelle sans effet : rien n'est levé (R1 B1).
        print(json.dumps({'agent': name, 'mode': 'execute',
                          'operator_paused': pause.exists(), 'changed': False,
                          'event_id': None}))
sys.exit(0)
'''

#: #2152 : plus aucune attente de créneau « idle » (DRAIN_ATTENTE est un
#: interrupteur) ; les attentes de bail restent instantanées dans le banc.
SLEEP = '#!/bin/sh\nexit 0\n'


class RedemarrageServeurDrainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-2152-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.home = self.root / 'home'
        (self.home / '.local/share/ameesh/venv/bin').mkdir(parents=True)
        cli = self.home / '.local/share/ameesh/venv/bin/ameesh'
        cli.write_text(FAKE_AMEESH)
        cli.chmod(0o755)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name, source in (('ssh', FAKE_SSH), ('sleep', SLEEP)):
            path = self.bin / name
            path.write_text(source)
            path.chmod(0o755)
        self.config = self.root / 'bascule.env'
        self.config.write_text('VM=10.77.0.2\nPERSONAS_VM=dev9\n')
        self.env = dict(os.environ, HOME=str(self.home), BASCULE_ENV=str(self.config),
                        FAKE_STATE=str(self.state),
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'])

    def run_vm(self, action='redemarrer'):
        return subprocess.run(['bash', str(SCRIPT), action], env=self.env,
                              capture_output=True, text=True, timeout=120)

    def poste(self):
        path = self.state / 'poste.log'
        return path.read_text() if path.exists() else ''

    def test_drain_poses_and_lifts_a_short_operator_pause(self):
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        journal = self.poste()
        self.assertIn('pause dev9', journal)
        self.assertIn('--reason drain de déploiement #2152 (vm.sh redemarrer sur 10.77.0.2)',
                      journal)
        # #2152 / R1 B1 : la levée est conditionnée au motif exact de la pose.
        pose = [l for l in journal.splitlines() if l.startswith('pause dev9')][0]
        reprise = [l for l in journal.splitlines() if l.startswith('resume dev9')][0]
        motif = pose.split('--reason ', 1)[1].split(' --human ', 1)[0]
        self.assertIn('--expect-reason ' + motif, reprise)
        self.assertIn('drain posé', p.stdout)
        self.assertIn('drain levé (redémarrage hors tour)', p.stdout)
        self.assertIn('reprise prouvée (operator_paused=false)', p.stdout)
        self.assertIn('dev9 : redémarré (était idle)', p.stdout)
        journal_ssh = (self.state / 'ssh.log').read_text()
        self.assertIn('systemctl stop ameesh-runner@dev9', journal_ssh)
        self.assertIn('systemctl start ameesh-runner@dev9', journal_ssh)
        self.assertNotIn('systemctl restart ameesh-runner@dev9', journal_ssh)
        self.assertFalse((self.state / 'pause-dev9').exists())

    def test_unsupported_cli_never_poses_a_pause(self):
        """N5 B1 : CLI antérieur à --expect-reason — aucune pause posée ;
        l'arrêt prouvé #1152 s'applique sans attendre une échéance."""
        self.env['DRAIN_ATTENTE'] = '1'
        (self.state / 'sans-expect-reason').touch()
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('drain #2152 indisponible', p.stderr)
        self.assertNotIn('toujours en tour', p.stderr)
        journal = self.poste()
        self.assertNotIn('pause dev9', journal)
        self.assertNotIn('resume dev9 --operator', journal)
        self.assertFalse((self.state / 'pause-dev9').exists())
        journal_ssh = (self.state / 'ssh.log').read_text()
        self.assertIn('systemctl stop ameesh-runner@dev9', journal_ssh)
        self.assertIn('systemctl start ameesh-runner@dev9', journal_ssh)

    def test_drain_carries_a_deadline_and_the_operator_actor(self):
        """#2184/R3 et N6 : la pose porte une échéance absolue et l'acteur
        humain du poste, jamais le compte `ameesh` de la VM."""
        self.env['DRAIN_ATTENTE'] = '1'
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        pose = [l for l in self.poste().splitlines() if l.startswith('pause dev9')][0]
        self.assertIn('--not-after ', pose)
        self.assertIn('--human human:', pose)
        self.assertIn('--reason drain de déploiement #2152', pose)

    def test_cli_without_not_after_never_poses_a_pause(self):
        """#2184/R3 : un CLI sans --not-after ne peut pas porter la barrière ;
        le script arrête sans drain plutôt que de risquer une pause tardive."""
        self.env['DRAIN_ATTENTE'] = '1'
        (self.state / 'sans-not-after').touch()
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('drain #2152 indisponible', p.stderr)
        self.assertNotIn('pause dev9', self.poste())
        self.assertFalse((self.state / 'pause-dev9').exists())
        journal_ssh = (self.state / 'ssh.log').read_text()
        self.assertIn('systemctl stop ameesh-runner@dev9', journal_ssh)
        self.assertIn('systemctl start ameesh-runner@dev9', journal_ssh)

    def test_foreign_pause_is_not_lifted(self):
        self.env['DRAIN_ATTENTE'] = '1'
        (self.state / 'pause-dev9').touch()
        (self.state / 'motif-dev9').write_text('pause humaine etrangere')
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('pause dev9', self.poste())
        self.assertNotIn('resume dev9', self.poste())
        self.assertTrue((self.state / 'pause-dev9').exists())
        self.assertIn('systemctl stop ameesh-runner@dev9',
                      (self.state / 'ssh.log').read_text())
        self.assertIn('systemctl start ameesh-runner@dev9',
                      (self.state / 'ssh.log').read_text())

    def test_non_execute_agent_is_skipped_in_server_mode(self):
        """#2198/N5 : etat_agent honore le mode aussi en mode serveur ; une
        persona passée `externe` n'est ni arrêtée ni redémarrée."""
        (self.state / 'mode-externe').touch()
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('dev9 : déplacé ou mode non execute (mode=externe), ignoré',
                      p.stdout)
        journal_ssh = (self.state / 'ssh.log').read_text()
        self.assertNotIn('systemctl stop ameesh-runner@dev9', journal_ssh)
        self.assertNotIn('systemctl start ameesh-runner@dev9', journal_ssh)
        self.assertNotIn('pause dev9', self.poste())


if __name__ == '__main__':
    unittest.main()
