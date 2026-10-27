# SPDX-License-Identifier: AGPL-3.0-only
"""Sequential drain (#4000) through the real Bash entry point, entirely fake SSH."""
from __future__ import annotations

import json
import re
import sys
import time
import unittest

from tests import support  # isolate the workstation environment
from tests import test_503_agent_host as host
from tests import test_2152_vm_drain as server


class HostWideDrainTest(unittest.TestCase):
    # Reuse the local host/CLI/systemd doubles, without rerunning their tests.
    setUp = host.AgentHostTest.setUp
    run_vm = host.AgentHostTest.run_vm
    units = host.AgentHostTest.units
    operateur = host.AgentHostTest.operateur

    def configure_turns(self, durations=(0, 0, 0), fail_second=False):
        self.env['DRAIN_ATTENTE'] = '1'
        clock = self.state / 'horloge'
        clock.write_text('100')
        self.env.update(HORLOGE_FICHIER=str(clock))
        self.agents = [dict(name=n, host='host-agents', mode='execute', status='running')
                       for n in ('dev1', 'dev2', 'dev3')]
        (self.state / 'durations.json').write_text(json.dumps(dict(zip(
            ('dev1', 'dev2', 'dev3'), durations))))
        cli = self.bin / 'ameesh'
        cli.write_text(host.AMEESH.replace(
            '        pause.touch()\n',
            "        pause.touch()\n"
            "        with (state / 'pauses.jsonl').open('a') as f:\n"
            "            f.write(json.dumps(sorted(p.name for p in state.glob('pause-*'))) + '\\n')\n"
            "        duration = json.loads((state / 'durations.json').read_text())[name]\n"
            "        (state / ('finish-' + name)).write_text(str(time.monotonic() + duration))\n"))
        unit = self.bin / 'systemctl'
        unit.write_text(host.SYSTEMCTL.replace('import os, sys\n', 'import os, sys, time\n').replace(
            "    if scenario == 'stop-error'",
            "    finish = state / ('finish-' + name)\n"
            "    if finish.exists():\n"
            "        time.sleep(max(0, float(finish.read_text()) - time.monotonic()))\n"
            + "    if finish.exists(): (state / ('finished-' + name)).touch()\n"
            + ("    if name == 'dev2': sys.exit(1)\n" if fail_second else '')
            + "    if scenario == 'stop-error'"))

    def assert_sequential_drains(self, names=('dev1', 'dev2', 'dev3')):
        events = (self.state / 'sequence.log').read_text().splitlines()
        self.assertEqual(events, [event + ' ' + name for name in names
                                  for event in ('pause', 'stop')])
        snapshots = [json.loads(line) for line in
                     (self.state / 'pauses.jsonl').read_text().splitlines()]
        self.assertTrue(snapshots)
        self.assertTrue(all(len(pauses) <= 1 for pauses in snapshots), snapshots)

    def assert_no_pause(self):
        self.assertEqual(list(self.state.glob('pause-*')), [])
        poses = [line.split()[1] for line in self.operateur().splitlines()
                 if line.startswith('pause ')]
        for name in poses:
            self.assertIn('resume ' + name + ' --operator', self.operateur())

    def stop_waits(self, p):
        """Attente réellement journalisée par arrêt prouvé, par persona."""
        return {m.group(1): int(m.group(2)) for m in re.finditer(
            r'ameesh-runner-agent@(dev\d)\.service : arrêt prouvé — attente (\d+) s',
            p.stdout)}

    def test_three_agents_are_drained_one_at_a_time_without_interrupting_turns(self):
        self.configure_turns((0.05, 0.1, 0.15))
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assert_sequential_drains()
        self.assert_no_pause()
        self.assertEqual(set(self.stop_waits(p)), {'dev1', 'dev2', 'dev3'}, p.stdout)
        for name in ('dev1', 'dev2', 'dev3'):
            self.assertTrue((self.state / ('finished-' + name)).exists())
            self.assertIn('--user start ameesh-runner-agent@' + name + '.service', self.units())
        self.assertNotIn('--user restart ', self.units())

    def test_idle_persona_is_only_paused_for_its_own_restart(self):
        self.configure_turns()
        self.agents[1]['status'] = 'idle'
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assert_sequential_drains()
        self.assert_no_pause()
        self.assertEqual(self.operateur().count('pause dev2 '), 1)

    def test_uncertain_prepose_is_reused_and_lifted(self):
        # #3120/B2 : la première pose (rc124) peut aboutir plus tard. Le
        # rattrapage doit réutiliser son motif, reconnaître la pause réelle et
        # la lever ; l'ancienne intention ne doit pas être remplacée ni perdue.
        self.configure_turns()
        self.agents[1]['status'] = 'idle'
        (self.state / 'pose-incertaine-agent').write_text('dev2')
        p = self.run_vm(scenario='pose-incertaine')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        poses = [l for l in self.operateur().splitlines() if l.startswith('pause dev2 ')]
        reprises = [l for l in self.operateur().splitlines() if l.startswith('resume dev2 ')]
        self.assertEqual(len(poses), 2, p.stdout + p.stderr)
        self.assertEqual(len(reprises), 1, p.stdout + p.stderr)
        motif = poses[0].split('--reason ', 1)[1].split(' --human ', 1)[0]
        self.assertIn('--expect-reason ' + motif, reprises[0])
        self.assertFalse((self.state / 'pause-dev2').exists())
        self.assert_no_pause()

    def test_uncertain_prepose_barrier_survives_a_later_success(self):
        # #3120/B3 : la seconde pose aboutit avant la première requête
        # incertaine ; sa barrière doit rester tenue jusqu'à l'échéance, sinon
        # la requête tardive repose le motif après la levée.
        self.configure_turns()
        self.agents[1]['status'] = 'idle'
        (self.state / 'pose-incertaine-agent').write_text('dev2')
        clock = self.state / 'horloge'
        clock.write_text('100')
        self.env.update(HORLOGE_FICHIER=str(clock), DRAIN_ECHEANCE='15',
                        DRAIN_BORNE_INSTRUCTION='1', DRAIN_DERIVE='1')
        p = self.run_vm(scenario='pose-incertaine-inverse')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        poses = [l for l in self.operateur().splitlines() if l.startswith('pause dev2 ')]
        reprises = [l for l in self.operateur().splitlines() if l.startswith('resume dev2 ')]
        self.assertEqual(len(poses), 2, p.stdout + p.stderr)
        self.assertEqual(len(reprises), 1, p.stdout + p.stderr)
        self.assertFalse((self.state / 'pose-incertaine-late-fait').exists(),
                         p.stdout + p.stderr)
        self.assertFalse((self.state / 'pause-dev2').exists())
        self.assert_no_pause()

    def test_second_stop_failure_cleans_every_pause(self):
        self.configure_turns(fail_second=True)
        p = self.run_vm()
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assert_sequential_drains(('dev1', 'dev2'))
        self.assert_no_pause()
        self.assertNotIn('--user start ameesh-runner-agent@dev2.service', self.units())
        self.assertNotIn('--user stop ameesh-runner-agent@dev3.service', self.units())

    def test_second_start_failure_leaves_no_pause(self):
        self.configure_turns()
        unit = self.bin / 'systemctl'
        unit.write_text(unit.read_text().replace(
            "elif 'start' in args:\n",
            "elif 'start' in args:\n    if name == 'dev2': sys.exit(1)\n"))
        p = self.run_vm()
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assert_sequential_drains(('dev1', 'dev2'))
        self.assert_no_pause()
        self.assertNotIn('pause dev3', self.operateur())

    def test_second_ssh_failure_reconciles_the_current_pause(self):
        self.configure_turns()
        ssh = self.bin / 'ssh'
        ssh.write_text(ssh.read_text().replace(
            "command = argv[-1]",
            "command = argv[-1]\n"
            "if 'systemctl' in command and 'stop ' in command and '@dev2.' in command:\n"
            "    sys.exit(255)"))
        p = self.run_vm()
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assert_no_pause()
        self.assertNotIn('pause dev3', self.operateur())

    def test_transient_resume_is_reconciled_before_the_next_agent(self):
        self.configure_turns()
        cli = self.bin / 'ameesh'
        cli.write_text(cli.read_text().replace(
            "        (state / 'resume-seen').touch()",
            "        (state / 'resume-seen').touch()\n"
            "        if name == 'dev1':\n"
            "            counter = state / 'resume-dev1-count'\n"
            "            count = int(counter.read_text()) if counter.exists() else 0\n"
            "            counter.write_text(str(count + 1))\n"
            "            if count < 2: sys.exit(1)"))
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assert_sequential_drains()
        self.assert_no_pause()
        self.assertEqual((self.state / 'resume-dev1-count').read_text(), '3')
        self.assertNotIn('NON levé', p.stdout + p.stderr)

    def test_unconfirmed_resume_prevents_the_next_pause_and_is_reported(self):
        self.configure_turns()
        cli = self.bin / 'ameesh'
        cli.write_text(cli.read_text().replace(
            "        (state / 'resume-seen').touch()",
            "        (state / 'resume-seen').touch()\n        sys.exit(1)"))
        p = self.run_vm()
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('drain précédent non réconcilié', p.stderr)
        self.assertIn('drain(s) NON levé(s) : dev1', p.stdout + p.stderr)
        self.assertNotIn('pause dev2', self.operateur())
        self.assertNotIn('pause dev3', self.operateur())
        self.assertEqual([p.name for p in self.state.glob('pause-*')], ['pause-dev1'])
        snapshots = [json.loads(line) for line in
                     (self.state / 'pauses.jsonl').read_text().splitlines()]
        self.assertTrue(all(len(pauses) <= 1 for pauses in snapshots), snapshots)

    def test_one_refused_pause_does_not_prevent_the_other_drains(self):
        self.configure_turns()
        cli = self.bin / 'ameesh'
        cli.write_text(cli.read_text().replace(
            "        motif.write_text(args[args.index('--reason') + 1])",
            "        if name == 'dev2': sys.exit(1)\n"
            "        motif.write_text(args[args.index('--reason') + 1])"))
        p = self.run_vm()
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn('@dev2.service', self.units())
        self.assertIn('--user start ameesh-runner-agent@dev1.service', self.units())
        self.assertIn('--user start ameesh-runner-agent@dev3.service', self.units())
        self.assertEqual(list(self.state.glob('pause-*')), [])
        self.assertEqual(self.operateur().count('pause dev2 '), 1)

    def test_later_registry_failure_cleans_the_already_posed_drain(self):
        self.configure_turns()
        cli = self.bin / 'ameesh'
        cli.write_text(cli.read_text().replace(
            "    name = sys.argv[2]\n    # #2198/N4",
            "    name = sys.argv[2]\n    if name == 'dev2': sys.exit(1)\n    # #2198/N4"))
        p = self.run_vm()
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('--user start ameesh-runner-agent@dev1.service', self.units())
        self.assertNotIn('@dev2.service', self.units())
        self.assertNotIn('pause dev3', self.operateur())
        self.assertIn('resume dev1 --operator', self.operateur())
        self.assertEqual(list(self.state.glob('pause-*')), [])

    def test_each_pose_renews_its_admission_deadline(self):
        self.configure_turns()
        clock = self.state / 'horloge'
        clock.write_text('100')
        self.env.update(HORLOGE_FICHIER=str(clock), DRAIN_ECHEANCE='1')
        cli = self.bin / 'ameesh'
        cli.write_text(cli.read_text().replace(
            '        pause.touch()\n',
            "        pause.touch()\n"
            "        with (state / 'pauses.jsonl').open('a') as f:\n"
            "            f.write(json.dumps(sorted(p.name for p in state.glob('pause-*'))) + '\\n')\n"
            "        clock = Path(os.environ['HORLOGE_FICHIER'])\n"
            "        clock.write_text(str(int(clock.read_text()) + 10))\n"))
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        poses = [line for line in self.operateur().splitlines() if line.startswith('pause dev')]
        self.assertEqual([line.split('--not-after ')[1].split()[0] for line in poses],
                         ['101', '111', '121'])
        self.assert_sequential_drains()
        self.assert_no_pause()


class ServerWideDrainTest(unittest.TestCase):
    setUp = server.RedemarrageServeurDrainTest.setUp
    run_vm = server.RedemarrageServeurDrainTest.run_vm
    poste = server.RedemarrageServeurDrainTest.poste

    def test_server_failure_only_pauses_the_current_persona(self):
        self.env['DRAIN_ATTENTE'] = '1'
        (self.bin / 'python3').symlink_to(sys.executable)
        self.config.write_text('VM=10.77.0.2\nPERSONAS_VM="dev1 dev2 dev3"\n')
        ssh = self.bin / 'ssh'
        # Reach the first stop with dev1's lease, then fail it deliberately.
        source = server.FAKE_SSH.replace('runner-dev9@mesh', 'runner-dev1@mesh')
        source = source.replace("    (state / 'stopped-dev9').touch()",
                                '    sys.exit(1)')
        ssh.write_text(source)
        cli = self.home / '.local/share/ameesh/venv/bin/ameesh'
        cli.write_text(server.FAKE_AMEESH.replace("'runner-dev9@mesh'", "'runner-' + name + '@mesh'"))
        p = self.run_vm()
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('pause dev1 ', self.poste())
        self.assertIn('resume dev1 --operator', self.poste())
        for name in ('dev1', 'dev2', 'dev3'):
            self.assertFalse((self.state / ('pause-' + name)).exists())
        for name in ('dev2', 'dev3'):
            self.assertNotIn('pause ' + name + ' ', self.poste())
        self.assertIn('systemctl stop ameesh-runner@dev1', (self.state / 'ssh.log').read_text())


if __name__ == '__main__':
    unittest.main()
