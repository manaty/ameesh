# SPDX-License-Identifier: AGPL-3.0-only
"""Native VM helpers, with native DB evidence and a simulated SSH guest."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import PgTestCase
from tests import test_6252_vm_commandes as cycle
from ameesh import mail, storage, undeliverable, vm_adapter as vm

cmd = cycle.cmd

ROOT = Path(__file__).resolve().parents[1]
REMOTE = ROOT / 'deploy/vm-adapter/remote'
spec = importlib.util.spec_from_file_location('vm_remote', REMOTE / 'vm-remote.py')
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)
NONCE = 'a' * 32


def config(state):
    cfg = json.loads((REMOTE / 'enrollment.example.json').read_text())
    cfg.update(host='burst', probe_agent='burst-probe', state_dir=str(state))
    return cfg


class DispatchTest(unittest.TestCase):
    def test_only_exact_enrolled_commands_are_accepted(self):
        for verb in ('reconcile', 'quiesce', 'runners', 'mail', 'doctor', 'version-mode'):
            self.assertEqual(remote.dispatch(remote.PREFIX + verb), (verb, []))
        self.assertEqual(remote.dispatch(shlex.join(remote.HALT)),
                         (remote.HALT[0], remote.HALT[1:]))
        self.assertEqual(remote.dispatch(shlex.join(remote.JOURNAL)),
                         (remote.JOURNAL[0], remote.JOURNAL[1:]))
        start = remote.PREFIX + 'probe-start burst burst-probe 1 0 ' + NONCE
        self.assertEqual(remote.dispatch(start)[1], ['burst', 'burst-probe', 1, 0, NONCE])
        for text in ('', 'sh', '/bin/sh -c true', 'sudo systemctl poweroff',
                     shlex.join(remote.HALT + ['--force']),
                     remote.PREFIX + 'reconcile extra', start + '; id',
                     start.replace(' 1 ', ' -1 '), start.replace(NONCE, '../bad'),
                     remote.PREFIX + '../reconcile', remote.PREFIX + 'mail\n',
                     remote.PREFIX + 'probe-receipt $(id)'):
            with self.subTest(text=text), self.assertRaises(remote.Refus):
                remote.dispatch(text)

    def test_refusal_occurs_before_any_connection_or_command(self):
        connect, run = mock.Mock(), mock.Mock()
        self.assertEqual(remote.main(['dispatch'], environ={'SSH_ORIGINAL_COMMAND': 'id'},
                                     connect=connect, runner=run), 1)
        connect.assert_not_called()
        run.assert_not_called()

    def test_enrollment_bounds_and_fixed_checks(self):
        cfg = config('/private/state')
        self.assertEqual(remote.enrollment(cfg), cfg)
        for key, value in (('host', 'x;id'), ('fresh_seconds', True),
                           ('fresh_seconds', 0), ('reconcile_deadline_s', 0),
                           ('reconcile_deadline_s', 3601), ('reconcile_deadline_s', True),
                           ('checks', {}), ('state_dir', '/a/../b')):
            with self.subTest(key=key), self.assertRaises(remote.Refus):
                remote.enrollment(dict(cfg, **{key: value}))

    def test_forced_health_uses_only_fixed_argv_and_scrubbed_environment(self):
        cfg = config('/private/state')
        cfg['checks']['doctor'] = ['/usr/bin/stat', '/etc/os-release']
        run, connect = mock.Mock(), mock.Mock()
        run.return_value.returncode = 0
        with mock.patch.object(remote, 'private_json', return_value=cfg), \
                mock.patch.object(remote.os, 'geteuid', return_value=1001):
            code = remote.main(['dispatch'],
                               environ={'SSH_ORIGINAL_COMMAND': remote.PREFIX + 'doctor',
                                        'CLOUD_SECRET': 'never-inherited', 'PATH': '/hostile'},
                               connect=connect, runner=run)
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], cfg['checks']['doctor'])
        self.assertEqual(run.call_args.kwargs['env'], remote.ENV)
        self.assertNotIn('shell', run.call_args.kwargs)
        connect.assert_not_called()

    def test_root_login_is_refused_even_for_an_admitted_command(self):
        cfg = config('/private/state')
        run = mock.Mock()
        with mock.patch.object(remote, 'private_json', return_value=cfg), \
                mock.patch.object(remote.os, 'geteuid', return_value=0):
            self.assertEqual(remote.main(['dispatch'], runner=run,
                             environ={'SSH_ORIGINAL_COMMAND': '/usr/bin/true'}), 1)
        run.assert_not_called()

    def test_state_requires_private_directory_and_refuses_links(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            store = remote.Store(tmp)
            with store.locked():
                with self.assertRaises(BlockingIOError):
                    with remote.Store(tmp).locked():
                        pass
                store.save(NONCE, {'nonce': NONCE})
            self.assertEqual(store.load(NONCE), {'nonce': NONCE})
            path = Path(tmp) / (NONCE + '.json')
            os.chmod(path, 0o644)
            with self.assertRaises(remote.Refus):
                store.load(NONCE)
            path.unlink()
            path.symlink_to('/etc/passwd')
            with self.assertRaises(OSError):
                store.load(NONCE)
            os.chmod(tmp, 0o755)
            with self.assertRaises(remote.Refus):
                remote.Store(tmp)


class NativeTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM vm_adapter_requests')
        self.db.execute('DELETE FROM host_lifecycle')
        self.db.execute('DELETE FROM turn_resources')
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.cfg_remote = config(self.temp.name)
        self.native = remote.Native(self.db, self.cfg_remote)
        self.endpoint = remote.Endpoint(self.cfg_remote, self.native, remote.Store(self.temp.name))
        self.db.execute("INSERT INTO agent_registry(name,harness,host,mode,status) "
                        "VALUES ('burst-probe','codex','burst','execute','idle')")
        self.db.execute("INSERT INTO host_lifecycle(host,mode,actor,claims_open,return_probe) "
                        "VALUES ('burst','returning','test',false,'burst-probe')")
        self.db.execute("INSERT INTO vm_adapter_requests(host,generation,release_epoch,state,status,result) "
                        "SELECT host,generation,release_epoch,'busy','pending','probe admitted' "
                        "FROM host_lifecycle WHERE host='burst'")
        row = self.db.query("SELECT generation,release_epoch FROM host_lifecycle WHERE host='burst'")[0]
        self.args = ['burst', 'burst-probe', row['generation'], row['release_epoch'], NONCE]

    def start(self):
        return self.endpoint.execute('probe-start', self.args)

    def receipt(self):
        return self.endpoint.execute('probe-receipt', [NONCE])[1]

    def delivered(self, *, done=False, ack=False):
        launch = self.endpoint.store.load(NONCE)
        self.db.execute("INSERT INTO turn_resources(turn_id,agent,host,status) "
                        "VALUES ('probe-turn','burst-probe','burst','running')")
        self.db.execute("UPDATE agent_mailbox SET delivered_at=clock_timestamp(),status='delivered', "
                        "meta='{\"consigne\":{\"turn\":\"probe-turn\"}}'::jsonb WHERE id=%s",
                        (launch['request_id'],))
        if ack:
            self.ack()
        if done:
            self.end()

    def ack(self, nonce=NONCE, sender='burst-probe'):
        undeliverable.check_send(self.cfg, self.db, sender=sender, dest='burst-probe')
        mail.send(self.db, sender, 'burst-probe', 'VM-PROBE-ACK ' + nonce,
                  host='burst', payload={'ack': True}, thread=False)

    def end(self):
        self.db.execute("UPDATE turn_resources SET status='done',ended_at=clock_timestamp() "
                        "WHERE turn_id='probe-turn'")

    def test_requested_ack_destination_passes_native_registry_check(self):
        self.start()
        body = self.db.query("SELECT body FROM agent_mailbox WHERE sender='vm-adapter'")[0]['body']
        command = body.split('with ', 1)[1].split(' then end', 1)[0]
        argv = shlex.split(command)
        self.assertEqual(argv, ['agent-mail', 'send', 'burst-probe', '--ack',
                                'VM-PROBE-ACK ' + NONCE])
        undeliverable.check_send(self.cfg, self.db, sender='burst-probe', dest=argv[2])
        self.delivered()
        mail.send(self.db, 'burst-probe', argv[2], argv[4], host='burst',
                  payload={'ack': True}, thread=False)
        self.end()
        self.assertEqual(self.receipt()['verdict'], 'ok')

    def test_start_is_idempotent_and_delivery_alone_is_never_completion(self):
        self.assertEqual(self.start(), (0, None))
        self.assertEqual(self.start(), (0, None))
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_mailbox")[0]['n'], 1)
        self.assertEqual(self.receipt()['verdict'], 'pending')
        self.delivered()
        proof = self.receipt()
        self.assertTrue(proof['delivered'])
        self.assertFalse(proof['acknowledged'])
        self.assertFalse(proof['turn_ended'])
        self.ack()
        self.assertEqual(self.receipt()['verdict'], 'pending')
        self.end()
        proof = self.receipt()
        self.assertEqual(proof['verdict'], 'ok')
        launch = self.endpoint.store.load(NONCE)
        self.assertEqual(cmd.receipt(json.dumps(proof).encode(), launch), 'ok')
        self.assertEqual(self.endpoint.execute('mail', []), (0, None))

    def test_wrong_nonce_or_sender_does_not_acknowledge_this_turn(self):
        self.start()
        self.delivered()
        self.ack('b' * 32)
        self.ack(sender='another-probe')
        self.end()
        self.assertFalse(self.receipt()['acknowledged'])
        self.assertEqual(self.receipt()['verdict'], 'ko')

    def test_lost_local_record_recovers_original_message_without_a_second_turn(self):
        with mock.patch.object(self.endpoint.store, 'save', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                self.start()
        request = self.db.query('SELECT id FROM agent_mailbox')[0]['id']
        # The runner may finish before the caller retries its lost answer.
        self.start()
        self.delivered(done=True, ack=True)
        launch = self.endpoint.store.load(NONCE)
        Path(self.temp.name, NONCE + '.json').unlink()
        self.start()
        self.assertEqual(self.endpoint.store.load(NONCE), launch)
        self.assertEqual(self.endpoint.store.load(NONCE)['request_id'], request)
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_mailbox WHERE sender='vm-adapter'")[0]['n'], 1)
        self.assertEqual(self.receipt()['verdict'], 'ok')

    def test_failed_turn_is_not_healthy(self):
        self.start()
        self.delivered(done=True, ack=True)
        self.db.execute("UPDATE agent_registry SET status='blocked',last_error='auth failure' "
                        "WHERE name='burst-probe'")
        self.assertEqual(self.receipt()['verdict'], 'ko')

    def test_readmission_and_new_epoch_invalidate_old_proof(self):
        self.start()
        self.delivered(done=True, ack=True)
        self.assertEqual(self.receipt()['verdict'], 'ok')
        self.db.execute("UPDATE vm_adapter_requests SET updated_at=clock_timestamp()")
        self.assertEqual(self.receipt()['verdict'], 'pending')
        self.db.execute("UPDATE host_lifecycle SET release_epoch=release_epoch+1 WHERE host='burst'")
        with self.assertRaises(remote.Refus):
            self.receipt()

    def test_reconcile_waits_for_runner_and_native_orphan_settlement_without_opening(self):
        self.assertEqual(self.endpoint.execute('reconcile', [])[0], 75)
        self.db.execute("UPDATE agent_registry SET runner_seen_at=clock_timestamp(), "
                        "runner_seen_host='burst',runner_seen_owner='runner-1' WHERE name='burst-probe'")
        self.assertEqual(self.endpoint.execute('reconcile', [])[0], 0)
        self.db.execute("INSERT INTO turn_resources(turn_id,agent,host,status) "
                        "VALUES ('old','burst-probe','burst','orphan')")
        self.assertEqual(self.endpoint.execute('reconcile', [])[0], 75)
        self.db.execute("UPDATE turn_resources SET status='done',ended_at=clock_timestamp() WHERE turn_id='old'")
        self.assertEqual(self.endpoint.execute('reconcile', [])[0], 0)
        self.db.execute("UPDATE agent_registry SET lease_owner='second-executor', "
                        "lease_expires_at=clock_timestamp()+interval '1 hour' WHERE name='burst-probe'")
        self.assertEqual(self.endpoint.execute('reconcile', [])[0], 75)
        control = self.db.query("SELECT mode,claims_open FROM host_lifecycle WHERE host='burst'")[0]
        self.assertEqual(control, {'mode': 'returning', 'claims_open': False})
        self.db.execute("UPDATE host_lifecycle SET mode='lost' WHERE host='burst'")
        with self.assertRaises(remote.Refus):
            self.endpoint.execute('reconcile', [])

    def test_foreign_generation_host_and_probe_are_refused(self):
        for index, value in ((0, 'other-host'), (1, 'other-probe'), (2, 900)):
            args = list(self.args)
            args[index] = value
            with self.subTest(index=index), self.assertRaises(remote.Refus):
                self.endpoint.execute('probe-start', args)
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM agent_mailbox')[0]['n'], 0)

    def test_reconciliation_deadline_fails_closed_and_is_scoped_to_this_wave(self):
        self.db.execute("UPDATE vm_adapter_requests SET updated_at=clock_timestamp()-interval '901 seconds'")
        with self.assertRaisesRegex(remote.Refus, 'deadline'):
            self.endpoint.execute('reconcile', [])
        self.db.execute("UPDATE vm_adapter_requests SET updated_at=clock_timestamp()")
        # A new admission gets its own deadline, but still needs evidence.
        self.assertEqual(self.endpoint.execute('reconcile', [])[0], 75)
        control = self.db.query("SELECT mode,claims_open FROM host_lifecycle WHERE host='burst'")[0]
        self.assertEqual(control, {'mode': 'returning', 'claims_open': False})


class NativeVM(cycle.FakeVM):
    def __init__(self, endpoint):
        super().__init__()
        self.endpoint = endpoint
        # Once, inside the remote call: after the worker's own local checks.
        self.halts, self.before = [], {}

    def run(self, argv, timeout, *, capture=False):
        args = shlex.split(argv[-1])
        name = args[0].rsplit('/', 1)[-1]
        if name not in ('probe-start', 'probe-receipt', 'reconcile', 'mail', 'quiesce', 'sudo'):
            return super().run(argv, timeout, capture=capture)
        if name in self.failing:
            return 1, b'', b''
        if name in self.unreachable:
            return self.unreachable[name], b'', b''
        self.calls.append(args)
        if name in self.before:
            self.before.pop(name)()
        if name == 'sudo':
            return self.poweroff(argv[-1])
        if name == 'probe-start':
            self.launches.append(args[1:])
            if self.on_start:
                self.on_start()
        try:
            verb, tail = remote.dispatch(argv[-1])
            code, value = self.endpoint.execute(verb, tail)
            return code, json.dumps(value).encode() if value is not None else b'', b''
        except remote.Refus:
            return 1, b'', b''

    def poweroff(self, command):
        """The forced-key entry itself: its native guard, then the fixed HALT."""
        def runner(argv, **kw):
            # main() swallows exceptions: record, the test asserts.
            self.halts.append((argv, kw['env']))
            if self.on_halt:
                self.on_halt()
            self.up, self.provider = False, 'stopped in place'
            return SimpleNamespace(returncode=0)
        db = self.endpoint.native.db
        class Connection:  # main() closes its connection; the test owns this one
            def __getattr__(self, name):
                return getattr(db, name)
            def close(self):
                pass
        with mock.patch.object(remote, 'private_json', return_value=self.endpoint.cfg), \
                mock.patch.object(remote.os, 'geteuid', return_value=1001), \
                mock.patch('ameesh.config.load_file', return_value=None):
            code = remote.main(['dispatch'], environ={'SSH_ORIGINAL_COMMAND': command},
                               connect=lambda _: Connection(), runner=runner)
        # The guest drops the connection while powering off.
        return (255 if not self.up else code), b'', b''


class NativeCycleTest(cycle.CloudCycleTest):
    """Reuse the real worker simulation with the real remote probe endpoints."""
    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM turn_resources')
        real_store = storage.of(self.db)
        overrides = self.store
        class StoreProxy:
            def __getattr__(self, name):
                return getattr(overrides, name, None) or getattr(real_store, name)
        self.store = StoreProxy()
        self.remote_temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.remote_temp.cleanup)
        self.remote_cfg = config(self.remote_temp.name)
        self.db.execute("INSERT INTO agent_registry(name,harness,host,mode,status,runner_seen_at, "
                        "runner_seen_host,runner_seen_owner) VALUES "
                        "('burst-probe','codex','burst','execute','idle',clock_timestamp(),'burst','runner-1')")
        self.endpoint = remote.Endpoint(self.remote_cfg, remote.Native(self.db, self.remote_cfg),
                                        remote.Store(self.remote_temp.name))
        self.vm = NativeVM(self.endpoint)

    def tick(self):
        original = storage.of
        def selected(db):
            return self.store if db is self.db else original(db)
        with mock.patch('ameesh.vm_adapter.storage.of', side_effect=selected):
            return vm.tick_host(self.cfg, self.db, self.policy, 'burst', self.vm, probe=self.probe,
                                evidence=lambda *a, **k: {'idle_since': k['now'] - 7200})

    def complete(self, **over):
        nonce = self.vm.launches[-1][-1]
        launch = self.endpoint.store.load(nonce)
        turn = 'native-' + nonce
        self.db.execute("DELETE FROM turn_resources WHERE turn_id=%s", (turn,))
        self.db.execute("INSERT INTO turn_resources(turn_id,agent,host,status) "
                        "VALUES (%s,'burst-probe','burst','running')", (turn,))
        self.db.execute("UPDATE agent_mailbox SET delivered_at=clock_timestamp(),status='delivered', "
                        "meta=jsonb_build_object('consigne',jsonb_build_object('turn',%s::text)) WHERE id=%s",
                        (turn, launch['request_id']))
        self.db.execute("DELETE FROM agent_mailbox WHERE sender='burst-probe' AND recipient='burst-probe'")
        if over.get('acknowledged', True):
            undeliverable.check_send(self.cfg, self.db, sender='burst-probe', dest='burst-probe')
            mail.send(self.db, 'burst-probe', 'burst-probe', 'VM-PROBE-ACK ' + nonce,
                      host='burst', payload={'ack': True}, thread=False)
        if over.get('turn_ended', True):
            self.db.execute("UPDATE turn_resources SET status='done',ended_at=clock_timestamp() WHERE turn_id=%s", (turn,))
        self.db.execute("UPDATE agent_registry SET status=%s,runner_seen_at=clock_timestamp() "
                        "WHERE name='burst-probe'",
                        ('blocked' if over.get('verdict') == 'ko' else 'idle',))
        # Parent's old-receipt negative test alters a proof. Change the native
        # message's binding instead, so the producer itself must refuse it.
        if over:
            value = dict(launch)
            for key, val in over.items():
                if key in value:
                    value[key] = val
            self.db.execute("UPDATE agent_mailbox SET payload=jsonb_build_object('vm_probe',%s::jsonb) WHERE id=%s",
                            (json.dumps({k: value[k] for k in ('host','probe_agent','generation','release_epoch','nonce')}),
                             launch['request_id']))

    def drained(self):
        control = self.q.control('burst')
        self.q.request('burst', control['generation'], control['release_epoch'], 'idle-drained')
        self.q.observe('burst', 'running', 90)
        self.db.execute("UPDATE vm_adapter_observations SET on_since=clock_timestamp()-interval '2 hours'")

    def running_turn(self):
        self.db.execute("INSERT INTO turn_resources(turn_id,agent,host,status) "
                        "VALUES ('late-turn','burst-probe','burst','running')")

    def test_native_quiesce_and_guard_pass_then_the_fixed_poweroff_runs(self):
        self.drained()
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertEqual(self.tick(), 'halt prepared')
        self.assertEqual(self.tick(), 'halt sent')
        self.assertEqual(self.vm.halts, [(remote.HALT, remote.ENV)])
        self.assertEqual([c[0] for c in self.vm.calls if c[0].endswith(('quiesce', 'sudo'))],
                         [remote.PREFIX + 'quiesce'] * 2 + ['/usr/bin/sudo'])

    def test_native_quiesce_waits_for_a_running_turn(self):
        self.drained()
        self.assertEqual(self.tick(), 'stop prepared')
        self.vm.before['quiesce'] = self.running_turn
        self.assertEqual(self.tick(), 'quiesce pending')
        self.assertNotIn('halt', self.verbs)
        self.assertEqual((self.vm.provider, self.vm.halts), ('running', []))

    def test_vm_guard_refuses_poweroff_when_a_turn_starts_after_quiesce(self):
        self.drained()
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertEqual(self.tick(), 'halt prepared')
        self.vm.before['sudo'] = self.running_turn
        # The worker never trusts the halt command's code: "halt sent" either
        # way. Only the guest staying up shows the VM guard refused.
        self.assertEqual(self.tick(), 'halt sent')
        self.assertEqual(self.verbs[-2:], ['quiesce', 'halt'])
        self.assertEqual((self.vm.provider, self.vm.up, self.vm.halts), ('running', True, []))
        self.assertEqual(self.tick(), 'halt pending')
        self.assertNotIn('halted', self.verbs)
