# SPDX-License-Identifier: AGPL-3.0-only
"""#6252: the five per-VM operator commands and a simulated cloud cycle.

No network, no secret, no real VM: ssh and the provider are fakes, the native
base is the test schema, and the worker is the real `vm_adapter.tick_host`."""
from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import PgTestCase
from tests.test_5925_vm_adapter import policy
from ameesh import storage, vm_adapter as vm
from ameesh.storage.postgres.vm_adapter import Queue

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / 'deploy' / 'vm-adapter'
SCRIPT = ADAPTER / 'commandes' / 'vm-commande.py'
_spec = importlib.util.spec_from_file_location('vm_commande', SCRIPT)
cmd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cmd)

FRESH = mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value={'sampled_ts': 1})


def example():
    return json.loads((ADAPTER / 'commandes' / 'cible.example.json').read_text())


class FakeVM:
    """One guest behind ssh, and its provider state."""

    def __init__(self):
        self.up, self.provider = True, 'running'
        self.calls, self.launches, self.receipt = [], [], None
        self.failing, self.unreachable = set(), {}
        self.on_halt = self.on_start = None

    def run(self, argv, timeout, *, capture=False):
        assert argv[0] == '/usr/bin/ssh' and argv[-3] == '--' and timeout <= 9.5
        remote = shlex.split(argv[-1])
        self.calls.append(remote)
        if not self.up:
            return 255, b'', b'ssh: connect to host 192.0.2.10 port 22: Connection timed out\n'
        name = remote[0].rsplit('/', 1)[-1]
        if name in self.unreachable:  # the transport, not the guest
            return self.unreachable[name], b'', b''
        if name in self.failing:
            return 1, b'', b''
        if name == 'sudo':
            if self.on_halt:
                self.on_halt()
            self.up, self.provider = False, 'stopped in place'
            return 255, b'', b''
        if name == 'probe-start':
            self.launches.append(remote[1:])
            if self.on_start:
                self.on_start()
            return 0, b'', b''
        if name == 'probe-receipt':
            if self.receipt is None:
                return 1, b'', b''
            return 0, (json.dumps(self.receipt) + '\n').encode(), b''
        if name == 'journalctl':
            return 0, b'systemd-journald[1]: Journal stopped\n', b''
        return 0, b'', b''

    def state(self, target):
        return self.provider

    def allowed(self, target):
        return {'poweron', 'poweroff'}

    def action(self, target, action):
        self.provider = {'poweroff': 'stopped', 'poweron': 'running'}[action]
        self.up = action == 'poweron'


class Files:
    """A private operator tree: cible.json, key, known_hosts, state dir."""

    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(dir=os.environ.get('TMPDIR')))
        self.addCleanup(shutil.rmtree, self.dir, True)
        state = self.dir / 'state'
        state.mkdir(mode=0o700)
        for name in ('key', 'known_hosts', 'ameesh.toml'):
            (self.dir / name).write_text('x')
            os.chmod(self.dir / name, 0o600)
        self.cible = example()
        self.cible.update(host='burst', probe_agent='burst-probe', state_dir=str(state),
                          ameesh_config=str(self.dir / 'ameesh.toml'))
        self.cible['ssh'].update(identity_file=str(self.dir / 'key'),
                                 known_hosts=str(self.dir / 'known_hosts'))
        self.path = self.dir / 'cible.json'
        self.write()
        self.vm = FakeVM()

    def write(self):
        self.path.write_text(json.dumps(self.cible))
        os.chmod(self.path, 0o600)

    def marker(self, name):
        return Path(self.cible['state_dir']) / ('%s.burst.json' % name)


class TargetTest(Files, unittest.TestCase):
    def test_example_is_valid_and_names_no_real_host(self):
        value = cmd.target(example())
        self.assertEqual(value['budget_s'], 9)
        text = (ADAPTER / 'commandes' / 'cible.example.json').read_text()
        self.assertNotIn('ameesh-agents', text)
        self.assertIn('192.0.2.', text)  # RFC 5737 documentation range

    def test_enrollment_example_points_at_the_five_commands(self):
        enrolled = json.loads((ADAPTER / 'enrollment.example.json').read_text())
        enrolled.update(enabled=True)
        target = vm.enrollment(enrolled, local_host=enrolled['controller_host'])['targets']['burst-host']
        for verb in cmd.VERBS:
            argv = target[verb + '_argv']
            self.assertEqual(argv[-2:], [verb, '/etc/ameesh-vm-adapter/cibles/burst-host.json'])
            self.assertTrue(argv[-3].endswith('/vm-commande.py'))

    def test_refusals(self):
        def bad(change):
            value = example()
            change(value)
            with self.assertRaises(cmd.Refus):
                cmd.target(value)
        bad(lambda v: v.pop('remote'))
        bad(lambda v: v.update(extra=1))
        bad(lambda v: v.update(budget_s=10))
        bad(lambda v: v.update(host='a b'))
        bad(lambda v: v['ssh'].update(destination='-oProxyCommand=x'))
        bad(lambda v: v['ssh'].update(user='Root;'))
        bad(lambda v: v['ssh'].update(identity_file='key'))
        bad(lambda v: v['ssh'].update(connect_timeout_s=9))
        bad(lambda v: v['remote'].update(halt=['/sbin/poweroff', '{host}']))
        bad(lambda v: v['remote'].update(health=[]))
        bad(lambda v: v['remote'].pop('alive'))
        bad(lambda v: v.update(previous_boot_end='('))
        bad(lambda v: v['remote'].update(halt=['poweroff']))
        bad(lambda v: v['remote'].update(health=[['/bin/true'], ['doctor']]))

    def test_private_files_required(self):
        os.chmod(self.path, 0o644)
        self.assertEqual(cmd.main(['quiesce', str(self.path)], natif=mock.Mock(),
                                  runner=self.vm.run), 1)
        self.write()
        os.chmod(self.dir / 'ameesh.toml', 0o644)
        self.assertEqual(cmd.main(['quiesce', str(self.path)], natif=mock.Mock(),
                                  runner=self.vm.run), 1)
        os.chmod(self.dir / 'ameesh.toml', 0o600)
        os.chmod(self.dir / 'key', 0o640)
        self.assertEqual(cmd.main(['quiesce', str(self.path)], natif=mock.Mock(),
                                  runner=self.vm.run), 1)
        self.assertEqual(self.vm.calls, [])
        self.assertEqual(cmd.main(['poweroff', str(self.path)]), 2)

    def test_ssh_is_fixed_and_children_inherit_no_secret(self):
        seen = []
        def runner(argv, timeout, *, capture=False):
            seen.append(argv)
            return 0, b'', b''
        natif = mock.Mock()
        natif.control.return_value = dict(mode='draining', claims_open=False)
        natif.running_turns.return_value = 0
        self.assertEqual(cmd.main(['quiesce', str(self.path)], natif=natif, runner=runner), 0)
        argv = seen[0]
        for option in ('BatchMode=yes', 'StrictHostKeyChecking=yes', 'IdentitiesOnly=yes',
                       'IdentityAgent=none', 'GlobalKnownHostsFile=/dev/null',
                       'UserKnownHostsFile=' + str(self.dir / 'known_hosts')):
            self.assertIn(option, argv)
        self.assertEqual(argv[1:3], ['-F', 'none'])
        self.assertEqual(argv[-3:-1], ['--', '192.0.2.10'])
        self.assertEqual(shlex.split(argv[-1]), ['/opt/ameesh-vm-adapter/remote/quiesce'])
        with mock.patch.dict(os.environ, {'SCW_SECRET_KEY': 'leak', 'AMEESH_DSN': 'leak'}), \
                mock.patch.object(cmd.subprocess, 'run',
                                  return_value=SimpleNamespace(returncode=0, stdout=b'',
                                                               stderr=b'')) as run:
            cmd.run(['/usr/bin/true'], 5)
        self.assertEqual(run.call_args.kwargs['env'], {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})

    def test_halted_is_a_witness_of_silence_only(self):
        def code(result):
            return cmd.main(['halted', str(self.path)],
                            runner=lambda argv, timeout, capture=False: result)
        self.assertEqual(code((None, b'', b'')), 0)  # no answer within the bound
        self.assertEqual(code((255, b'', b'ssh: connect to host x port 22: Connection timed out')), 0)
        self.assertEqual(code((0, b'', b'')), 1)  # still answers
        self.assertEqual(code((255, b'', b'user@x: Permission denied (publickey).')), 1)
        self.assertEqual(code((255, b'', b'Host key verification failed.')), 1)
        self.assertEqual(code(('unstarted', b'', b'')), 1)  # no ssh binary
        self.cible['ssh']['known_hosts'] = str(self.dir / 'absent')
        self.write()
        self.assertEqual(code((None, b'', b'')), 1)

    def test_halt_writes_its_marker_before_the_guest_poweroff(self):
        natif = mock.Mock()
        natif.control.return_value = dict(mode='draining', claims_open=False)
        natif.running_turns.return_value = 0
        natif.now.return_value = 1000.0
        self.vm.on_halt = lambda: self.assertTrue(self.marker('halt').exists())
        self.assertEqual(cmd.main(['halt', str(self.path)], natif=natif, runner=self.vm.run), 0)
        self.assertEqual(self.vm.calls, [['/usr/bin/sudo', '-n', '/usr/bin/systemctl',
                                          'poweroff', '--no-block']])
        # Never on an open gate, a return, or with a turn running.
        for control, turns in ((dict(mode='draining', claims_open=True), 0),
                               (dict(mode='returning', claims_open=False), 0),
                               (dict(mode='draining', claims_open=False), 1)):
            natif.control.return_value, natif.running_turns.return_value = control, turns
            self.assertEqual(cmd.main(['halt', str(self.path)], natif=natif, runner=self.vm.run), 1)
        self.assertEqual(len(self.vm.calls), 1)

    def test_a_held_vm_lock_is_a_wait_without_effect(self):
        import fcntl
        lock = self.marker('lock')
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        natif = mock.Mock()
        natif.control.return_value = dict(mode='draining', claims_open=False)
        natif.running_turns.return_value = 0
        self.assertEqual(cmd.main(['health', str(self.path)], natif=natif, runner=self.vm.run),
                         cmd.WAIT)
        for verb in ('quiesce', 'halt', 'halted', 'reconcile'):
            self.assertEqual(cmd.main([verb, str(self.path)], natif=natif, runner=self.vm.run), 1)
        self.assertEqual(self.vm.calls, [])
        natif.control.assert_not_called()
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(cmd.main(['quiesce', str(self.path)], natif=natif, runner=self.vm.run), 0)

    def test_receipt_contract(self):
        launch = dict(host='burst', probe_agent='p', generation=3, release_epoch=2, nonce='a' * 32)
        full = dict(launch, verdict='ok', delivered=True, acknowledged=True, turn_ended=True)
        def verdict(value):
            return cmd.receipt((json.dumps(value) + '\n').encode(), launch)
        self.assertEqual(verdict(full), 'ok')
        self.assertEqual(verdict(dict(full, verdict='ko')), 'ko')
        self.assertEqual(verdict(dict(full, acknowledged=False)), 'pending')  # delivery alone
        self.assertEqual(verdict(dict(full, turn_ended=False)), 'pending')
        self.assertEqual(verdict(dict(full, delivered='true')), 'pending')
        for key, old in (('nonce', 'b' * 32), ('generation', 2), ('release_epoch', 1),
                         ('probe_agent', 'other'), ('host', 'h')):
            self.assertEqual(verdict(dict(full, **{key: old})), 'pending')
        self.assertEqual(verdict(dict(full, verdict='ko', nonce='b' * 32)), 'pending')
        self.assertEqual(cmd.receipt(b'not json', launch), 'pending')
        self.assertEqual(cmd.receipt(b'', launch), 'pending')

    def test_script_runs_isolated_and_silent(self):
        done = subprocess.run([sys.executable, '-I', str(SCRIPT), 'health', str(self.dir / 'absent')],
                              capture_output=True, env={'PATH': '/usr/bin:/bin'}, timeout=60)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, b'', b''))

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze unavailable')
    def test_units_verify(self):
        unit = (ADAPTER / 'ameesh-vm-adapter.service').read_text()
        self.assertIn('StateDirectory=ameesh-vm-adapter', unit)
        # Only the two placeholders the README asks the operator to replace.
        unit = unit.replace('User=REPLACE_WITH_EXISTING_OPERATOR_ACCOUNT',
                            'User=' + os.environ.get('USER', 'root')).replace(
            '/opt/ameesh-vm-adapter/venv/bin/python', sys.executable)
        (self.dir / 'ameesh-vm-adapter.service').write_text(unit)
        shutil.copy(ADAPTER / 'ameesh-vm-adapter.timer', self.dir)
        done = subprocess.run(['systemd-analyze', 'verify', 'ameesh-vm-adapter.service',
                               'ameesh-vm-adapter.timer'], cwd=self.dir,
                              capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr.decode(errors='replace'))


class CloudCycleTest(Files, PgTestCase):
    """The real worker tick, the real commands, a simulated VM and provider."""

    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM vm_adapter_requests')
        self.db.execute('DELETE FROM vm_adapter_observations')
        self.db.execute('DELETE FROM host_lifecycle')
        self.db.execute("INSERT INTO host_lifecycle(host,mode,actor,claims_open,release_state,hook_state) "
                        "VALUES ('burst','draining','tester',false,'draining','draining')")
        self.q = Queue(self.db)
        self.cfg = replace(self.cfg, idle_after_s=1800, host_release_adapter_stall=900)
        operations = mock.Mock()
        operations.listing.return_value = []
        operations.open_lots_activity.return_value = []
        hosts = mock.Mock()
        hosts.holds.return_value = []
        self.store = SimpleNamespace(host_lifecycle=storage.of(self.db).host_lifecycle,
                                     operations=operations, hosts=hosts)
        self.policy = policy()
        for verb in cmd.VERBS:
            self.policy['targets']['burst'][verb + '_argv'] = [
                '/opt/ameesh-vm-adapter/venv/bin/python', '-I',
                '/opt/ameesh-vm-adapter/commandes/vm-commande.py', verb, str(self.path)]
        self.verbs = []

    def probe(self, argv, timeout, *, pending=False):
        """`run_probe` for the enrolled argv, with the fake guest behind ssh."""
        self.assertEqual(timeout, 10)
        self.verbs.append(argv[-2])
        code = cmd.main(argv[-2:], natif=cmd.Natif.of(self.db), runner=self.vm.run)
        if pending and code == cmd.WAIT:
            return 'pending'
        return code == 0

    def tick(self):
        with mock.patch('ameesh.vm_adapter.storage.of', return_value=self.store):
            return vm.tick_host(self.cfg, self.db, self.policy, 'burst', self.vm, probe=self.probe,
                                evidence=lambda *a, **k: {'idle_since': k['now'] - 7200})

    def control(self):
        control = self.q.control('burst')
        return control['mode'], control['claims_open'], self.q.return_probe('burst')

    def request(self, state):
        return self.db.query("SELECT * FROM vm_adapter_requests WHERE state=%s "
                             "ORDER BY generation DESC LIMIT 1", (state,))[0]

    def returning(self):
        control = self.q.control('burst')
        self.q.request('burst', control['generation'], control['release_epoch'], 'busy')
        self.q.observe('burst', 'running', 90)
        self.db.execute("UPDATE vm_adapter_observations SET on_since=clock_timestamp()-interval '2 hours'")

    def complete(self, **over):
        nonce = self.vm.launches[-1][-1]
        control = self.q.control('burst')
        self.vm.receipt = dict(dict(host='burst', probe_agent='burst-probe', nonce=nonce,
                                    generation=control['generation'],
                                    release_epoch=control['release_epoch'], verdict='ok',
                                    delivered=True, acknowledged=True, turn_ended=True), **over)

    def test_halt_then_proof_then_poweroff(self):
        control = self.q.control('burst')
        self.q.request('burst', control['generation'], control['release_epoch'], 'idle-drained')
        self.q.observe('burst', 'running', 90)
        self.db.execute("UPDATE vm_adapter_observations SET on_since=clock_timestamp()-interval '2 hours'")
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertEqual(self.tick(), 'halt prepared')
        self.assertEqual(self.tick(), 'halt sent')
        self.assertEqual(self.vm.provider, 'stopped in place')
        self.assertTrue(self.marker('halt').exists())
        self.assertEqual(self.tick(), 'halt proven')
        self.assertEqual(self.tick(), 'archiving')
        self.assertEqual(self.vm.provider, 'stopped')
        self.assertEqual(self.tick(), 'stopped')
        self.assertEqual(self.verbs, ['quiesce', 'quiesce', 'halt', 'halted'])
        self.assertEqual(sum(c[0].endswith('sudo') for c in self.vm.calls), 1)
        self.assertEqual(self.request('idle-drained')['status'], 'done')
        self.assertEqual(self.control()[:2], ('draining', False))

    def test_quiesce_failure_is_a_wait_without_halt(self):
        control = self.q.control('burst')
        self.q.request('burst', control['generation'], control['release_epoch'], 'idle-drained')
        self.q.observe('burst', 'running', 90)
        self.db.execute("UPDATE vm_adapter_observations SET on_since=clock_timestamp()-interval '2 hours'")
        self.vm.failing.add('quiesce')
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertEqual(self.tick(), 'quiesce pending')
        self.assertEqual(self.tick(), 'quiesce pending')
        self.assertEqual(self.vm.provider, 'running')
        self.assertNotIn('halt', self.verbs)

    def test_probe_waits_75_then_ready_on_its_complete_receipt(self):
        self.returning()
        self.cible_marker()
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'health pending')  # launched, 75
            self.assertEqual(len(self.vm.launches), 1)
            control = self.q.control('burst')
            self.assertEqual(self.vm.launches[0][:4], ['burst', 'burst-probe',
                                                       str(control['generation']),
                                                       str(control['release_epoch'])])
            self.assertEqual(self.tick(), 'health pending')  # no receipt yet
            self.complete(nonce='0' * 32)
            self.assertEqual(self.tick(), 'health pending')  # an old receipt
            self.complete(generation=control['generation'] - 1)
            self.assertEqual(self.tick(), 'health pending')  # another generation
            self.complete(acknowledged=False, turn_ended=False, verdict='pending')
            self.assertEqual(self.tick(), 'health pending')  # delivery alone
            self.assertEqual(self.control(), ('returning', False, 'burst-probe'))
            self.complete()
            self.assertEqual(self.tick(), 'ready')
        self.assertEqual(len(self.vm.launches), 1)  # launched once, asynchronously
        self.assertIn(['/usr/bin/journalctl', '-b', '-1', '-n', '1', '-o', 'cat', '--no-pager'],
                      self.vm.calls)
        self.assertFalse(self.marker('halt').exists())
        self.assertEqual(self.request('busy')['status'], 'done')
        self.assertFalse(self.q.control('burst')['claims_open'])

    def cible_marker(self):
        self.marker('halt').write_text(json.dumps({'host': 'burst', 'at': 1}))
        os.chmod(self.marker('halt'), 0o600)

    def test_concurrent_invocations_launch_one_probe(self):
        # #6252 B (cx1): health runs before the worker lock, so two worker
        # invocations may decide at once. The second one, arriving while the
        # first holds the VM between its decision and its launch, waits.
        self.returning()
        seen = []
        argv = self.policy['targets']['burst']['health_argv']
        def second():
            seen.append(cmd.main(argv[-2:], natif=cmd.Natif.of(self.db), runner=self.vm.run))
            seen.append(cmd.main(['reconcile', str(self.path)], natif=cmd.Natif.of(self.db),
                                 runner=self.vm.run))
        self.vm.on_start = second
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'health pending')
        self.assertEqual(seen, [cmd.WAIT, 1])
        self.assertEqual(len(self.vm.launches), 1)
        self.vm.on_start = None
        with FRESH:
            self.assertEqual(self.tick(), 'health pending')  # same launch, no second one
            self.complete()
            self.assertEqual(self.tick(), 'ready')
        self.assertEqual(len(self.vm.launches), 1)

    def test_transport_cut_after_the_receipt_is_a_wait(self):
        # #6252 B (impl-03): no answer from ssh says nothing about the guest.
        self.returning()
        self.cible_marker()
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'health pending')
            self.complete()
            for cut in (None, 255, 'unstarted'):
                self.vm.unreachable = {'runners': cut}
                self.assertEqual(self.tick(), 'health pending')
                self.assertEqual(self.control(), ('returning', False, 'burst-probe'))
            self.vm.unreachable = {'journalctl': None}
            self.assertEqual(self.tick(), 'health pending')
            self.assertTrue(self.marker('halt').exists())
            self.vm.unreachable = {}
            self.assertEqual(self.tick(), 'ready')
        self.assertEqual(len(self.vm.launches), 1)

    def test_unclean_previous_boot_fails_health(self):
        self.returning()
        self.cible_marker()
        self.cible['previous_boot_end'] = 'Reached target System Power Off'
        self.write()
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'health pending')
            self.complete()
            self.assertEqual(self.tick(), 'health failed')
        self.assertEqual(self.control(), ('draining', False, None))

    def test_health_failure_goes_back_to_draining_then_one_new_probe(self):
        self.returning()
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'health pending')
            self.complete(verdict='ko')
            self.assertEqual(self.tick(), 'health failed')
            self.assertEqual(self.control(), ('draining', False, None))
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'health pending')
            self.assertEqual(len(self.vm.launches), 2)
            self.assertNotEqual(self.vm.launches[0][-1], self.vm.launches[1][-1])
            # The failed receipt of the first launch never answers the second.
            self.assertEqual(self.tick(), 'health pending')
            self.vm.failing.add('doctor')
            self.complete()
            self.assertEqual(self.tick(), 'health failed')  # a red doctor
        self.assertEqual(self.control(), ('draining', False, None))

    def test_reconcile_failure_keeps_claims_closed(self):
        self.returning()
        self.vm.failing.add('reconcile')
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'health pending')
            self.complete()
            self.assertEqual(self.tick(), 'reconciliation pending')
            self.assertEqual(self.control(), ('returning', False, 'burst-probe'))
            self.vm.failing.clear()
            self.assertEqual(self.tick(), 'ready')
        self.assertEqual(len(self.vm.launches), 1)
        self.assertFalse(self.q.control('burst')['claims_open'])


if __name__ == '__main__':
    unittest.main()
