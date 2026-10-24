# SPDX-License-Identifier: AGPL-3.0-only
"""Host-wide drain through the real Bash entry point, entirely fake SSH."""
from __future__ import annotations

import json
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
        self.agents = [dict(name=n, host='host-agents', mode='execute', status='running')
                       for n in ('dev1', 'dev2', 'dev3')]
        (self.state / 'durations.json').write_text(json.dumps(dict(zip(
            ('dev1', 'dev2', 'dev3'), durations))))
        cli = self.bin / 'ameesh'
        cli.write_text(host.AMEESH.replace(
            '        pause.touch()\n',
            "        pause.touch()\n"
            "        duration = json.loads((state / 'durations.json').read_text())[name]\n"
            "        (state / ('finish-' + name)).write_text(str(time.monotonic() + duration))\n"))
        unit = self.bin / 'systemctl'
        unit.write_text(host.SYSTEMCTL.replace('import os, sys\n', 'import os, sys, time\n').replace(
            "    if scenario == 'stop-error'",
            "    finish = state / ('finish-' + name)\n"
            "    if finish.exists():\n"
            "        time.sleep(max(0, float(finish.read_text()) - time.monotonic()))\n"
            + ("    if name == 'dev2': sys.exit(1)\n" if fail_second else '')
            + "    if scenario == 'stop-error'"))

    def assert_all_paused_before_stop(self):
        events = (self.state / 'sequence.log').read_text().splitlines()
        self.assertEqual(events[:3], ['pause dev1', 'pause dev2', 'pause dev3'])
        self.assertEqual(events[3], 'stop dev1')

    def assert_no_pause(self):
        self.assertEqual(list(self.state.glob('pause-*')), [])
        for name in ('dev1', 'dev2', 'dev3'):
            self.assertIn('resume ' + name + ' --operator', self.operateur())

    def test_three_turns_overlap_instead_of_adding(self):
        self.configure_turns((10, 20, 30))
        start = time.monotonic()
        p = self.run_vm()
        elapsed = time.monotonic() - start
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assert_all_paused_before_stop()
        self.assert_no_pause()
        print(f"#3114 tours 10/20/30 s : balayage {elapsed:.1f} s", flush=True)
        self.assertGreaterEqual(elapsed, 30)
        # Allow process/SSH double overhead on busy CI hosts; serial = 60+ s.
        self.assertLess(elapsed, 55, p.stdout)
        for name in ('dev1', 'dev2', 'dev3'):
            self.assertIn('ameesh-runner-agent@' + name + '.service : arrêt prouvé — attente ',
                          p.stdout)

    def test_second_stop_failure_cleans_every_pause(self):
        self.configure_turns(fail_second=True)
        p = self.run_vm()
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assert_all_paused_before_stop()
        self.assert_no_pause()
        self.assertNotIn('--user start ameesh-runner-agent@dev2.service', self.units())
        self.assertNotIn('--user stop ameesh-runner-agent@dev3.service', self.units())

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
        self.assertEqual(self.units(), '')
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
            "        clock = Path(os.environ['HORLOGE_FICHIER'])\n"
            "        clock.write_text(str(int(clock.read_text()) + 10))\n"))
        p = self.run_vm()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        poses = [line for line in self.operateur().splitlines() if line.startswith('pause dev')]
        self.assertEqual([line.split('--not-after ')[1].split()[0] for line in poses],
                         ['101', '111', '121'])
        self.assert_all_paused_before_stop()
        self.assert_no_pause()


class ServerWideDrainTest(unittest.TestCase):
    setUp = server.RedemarrageServeurDrainTest.setUp
    run_vm = server.RedemarrageServeurDrainTest.run_vm
    poste = server.RedemarrageServeurDrainTest.poste

    def test_server_also_pauses_every_persona_before_first_stop(self):
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
        lines = self.poste().splitlines()
        for name in ('dev1', 'dev2', 'dev3'):
            self.assertIn('pause ' + name + ' ', self.poste())
            self.assertIn('resume ' + name + ' --operator', self.poste())
            self.assertFalse((self.state / ('pause-' + name)).exists())
        # All three pause calls precede the first lease read / stop attempt.
        last_pause = max(i for i, line in enumerate(lines) if line.startswith('pause dev'))
        self.assertTrue(any(line.startswith('show dev1 ') for line in lines[last_pause + 1:]))
        self.assertIn('systemctl stop ameesh-runner@dev1', (self.state / 'ssh.log').read_text())


if __name__ == '__main__':
    unittest.main()
