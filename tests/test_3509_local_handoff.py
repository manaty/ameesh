# SPDX-License-Identifier: AGPL-3.0-only
"""H8 authority boundary on real unprivileged Unix socket pairs, no host effects."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour'


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dispatch = load('dispatch-local-handoff')
bridge = load('pont-handoff')


def request():
    return {'step': 'stop_source', 'host': 'source', 'operation': 'a' * 32,
            'action_id': 'a' * 32 + ':stop_source', 'controller_epoch': 3,
            'agent': 'dev', 'owner': 'runner', 'lease_epoch': 7}


class LocalAuthorityTest(unittest.TestCase):
    def setUp(self):
        self.rules = {'uid': os.getuid(), 'host': 'source', 'agents': ['dev']}

    def exchange(self, data, *, rules=None, execute=None):
        client, server = socket.socketpair()
        calls, errors = [], []
        def effect(value):
            calls.append(value)
            return {'host': 'source', 'action_id': value['action_id'],
                    'controller_epoch': 3, 'ok': False, 'reason': 'identity'}
        def run():
            with server:
                try:
                    dispatch.serve(server, rules or self.rules, execute or effect)
                except Exception as exc:
                    errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        with client:
            try:
                client.sendall(data)
                client.shutdown(socket.SHUT_WR)
                output = client.recv(65537)
            except (BrokenPipeError, ConnectionResetError):
                output = b''
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        return output, calls, errors

    def test_exact_receipt_and_single_dispatch(self):
        output, calls, errors = self.exchange(json.dumps(request()).encode())
        self.assertEqual(errors, [])
        self.assertEqual(calls, [request()])
        self.assertIs(json.loads(output)['ok'], False)

    def test_kernel_peer_uid_refuses_before_effect(self):
        output, calls, errors = self.exchange(json.dumps(request()).encode(),
            rules=dict(self.rules, uid=os.getuid() + 1))
        self.assertFalse(output)
        self.assertFalse(calls)
        self.assertTrue(errors)

    def test_unadmitted_identity_and_command_fields_refused(self):
        for changes in ({'host': 'other'}, {'agent': 'other'},
                        {'command': 'id'}, {'step': 'shell'}, {'lease_epoch': True}):
            with self.subTest(changes=changes):
                _, calls, errors = self.exchange(json.dumps(dict(request(), **changes)).encode())
                self.assertFalse(calls)
                self.assertTrue(errors)

    def test_duplicate_trailing_and_oversized_json_refused(self):
        valid = json.dumps(request()).encode()
        for data in (b'{"step":"inventory","step":"stop_source"}',
                     valid + valid, b' ' * 65537, b'[]'):
            _, calls, errors = self.exchange(data)
            self.assertFalse(calls)
            self.assertTrue(errors)

    def test_bad_or_lost_receipt_never_fabricates_success(self):
        for effect in (lambda value: {'ok': True},
                       mock.Mock(side_effect=OSError('lost after effect'))):
            output, _, errors = self.exchange(json.dumps(request()).encode(), execute=effect)
            self.assertFalse(output)
            self.assertTrue(errors)

    def test_no_inprocess_local_effect_fallback(self):
        local = bridge.LocalDispatcher()
        with mock.patch.object(local, '_exchange', side_effect=bridge.BridgeError('local')):
            channel = bridge.Bridge(local_host='source', transports={}, local=local)
            with self.assertRaises(bridge.BridgeError):
                channel.perform('source', request())

    def test_root_chain_rejects_agent_checkout(self):
        with self.assertRaises((dispatch.Refused, OSError)):
            dispatch.protected(ROOT / 'dispatch-local-handoff.py')

    def test_deadline_closes_stalled_reader(self):
        client, server = socket.socketpair()
        try:
            with mock.patch.object(dispatch, 'SECONDS', 0.01):
                with self.assertRaises(TimeoutError):
                    dispatch.serve(server, self.rules, mock.Mock())
        finally:
            client.close()
            server.close()


class AlarmOutcomeTest(unittest.TestCase):
    def test_dispatcher_alarm_preserves_intent_then_reconciliation_quiesces(self):
        import io
        import signal
        from .test_3470_handoff_activation import EffectTest, companion
        fixture = EffectTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepared()
        self.assertIs(fixture.call('admit')['ok'], True)
        handlers = {}
        channel = mock.Mock(family=socket.AF_UNIX, type=socket.SOCK_STREAM)
        import struct
        channel.getsockopt.return_value = struct.pack('3i', 42, 1001, 1001)
        observed = []

        def start(*args):
            # The unit started, but the alarm interrupts its response.
            fixture.system.fields['ActiveState'] = 'active'
            handlers[signal.SIGALRM](signal.SIGALRM, None)

        def serve(*args):
            try:
                fixture.call('enable')
            except companion.Uncertain as exc:
                observed.append(exc)
                raise

        with mock.patch.object(dispatch, 'executor', return_value=companion), \
                mock.patch.object(dispatch, 'policy', return_value={}), \
                mock.patch.object(dispatch, 'consistent_policy'), \
                mock.patch.object(companion, 'load_execution', return_value={}), \
                mock.patch.object(dispatch, 'os',
                    mock.Mock(geteuid=mock.Mock(return_value=0),
                              devnull=os.devnull,
                              dup=mock.Mock(return_value=123))), \
                mock.patch.object(dispatch.socket, 'socket', return_value=channel), \
                mock.patch.object(dispatch.signal, 'signal',
                    side_effect=lambda sig, handler: handlers.update({sig: handler})), \
                mock.patch.object(dispatch.signal, 'alarm'), \
                mock.patch.object(dispatch, 'serve', side_effect=serve), \
                mock.patch.object(fixture.system, 'start', side_effect=start), \
                mock.patch.object(dispatch.sys, 'stderr', new_callable=io.StringIO) as log:
            self.assertEqual(dispatch.main([]), 1)
        self.assertEqual(len(observed), 1, log.getvalue())
        self.assertEqual(str(observed[0]), 'deadline')
        self.assertEqual(log.getvalue(), 'local_handoff_uncertain pid=42 uid=1001\n')
        directory = fixture.fx.effects.operations / ('a' * 32)
        self.assertTrue((directory / 'enable.intent.json').exists())
        proof = fixture.call('reconcile_effect', controller_epoch=4)
        self.assertIs(proof['ok'], True)
        self.assertIs(proof['quiesced'], True)
        self.assertEqual(proof['epoch'], 3)
        self.assertGreaterEqual(fixture.system.stops, 1)
        self.assertEqual(fixture.call('enable', controller_epoch=3)['reason'],
                         'revoked_epoch')


class ContractOutcomeTest(unittest.TestCase):
    def main(self, error, *, executed):
        import io
        import struct
        module = mock.Mock()
        module.Uncertain = type('Uncertain', (Exception,), {})
        channel = mock.Mock(family=socket.AF_UNIX, type=socket.SOCK_STREAM)
        channel.getsockopt.return_value = struct.pack('3i', 42, 1001, 1001)

        def serve(channel, rules, effect):
            if executed:
                effect(request())
            raise error

        with mock.patch.object(dispatch.os, 'geteuid', return_value=0), \
                mock.patch.object(dispatch.os, 'dup', return_value=123), \
                mock.patch.object(dispatch, 'executor', return_value=module), \
                mock.patch.object(dispatch, 'policy', return_value={}), \
                mock.patch.object(dispatch, 'consistent_policy'), \
                mock.patch.object(dispatch.signal, 'signal'), \
                mock.patch.object(dispatch.signal, 'alarm'), \
                mock.patch.object(dispatch.socket, 'socket', return_value=channel), \
                mock.patch.object(dispatch, 'serve', side_effect=serve), \
                mock.patch.object(dispatch.sys, 'stderr', new_callable=io.StringIO) as log:
            self.assertEqual(dispatch.main([]), 1)
        return log.getvalue()

    def test_contract_error_after_execute_is_uncertain(self):
        error = dispatch.handoff_contract.ContractError('receipt')
        self.assertEqual(self.main(error, executed=True),
                         'local_handoff_uncertain pid=42 uid=1001\n')

    def test_contract_error_before_execute_is_refused(self):
        error = dispatch.handoff_contract.ContractError('secret')
        self.assertEqual(self.main(error, executed=False),
                         'local_handoff_refused pid=42 uid=1001\n')


class ClientBoundaryTest(unittest.TestCase):
    def channel(self, uid=0, output=b'{}'):
        import struct
        channel = mock.MagicMock()
        channel.__enter__.return_value = channel
        channel.getsockopt.return_value = struct.pack('3i', 42, uid, 1)
        channel.recv.side_effect = [output, b'']
        return channel

    def info(self, path):
        import stat
        from types import SimpleNamespace
        return SimpleNamespace(st_uid=0, st_mode=(stat.S_IFSOCK | 0o660)
            if str(path).endswith('local.sock') else (stat.S_IFDIR | 0o755))

    def test_nonroot_server_gets_no_request(self):
        channel = self.channel(uid=12345)
        with mock.patch.object(Path, 'lstat', lambda path: self.info(path)), \
                mock.patch.object(bridge.socket, 'socket', return_value=channel):
            with self.assertRaisesRegex(bridge.BridgeError, 'local_peer'):
                bridge.LocalDispatcher().perform(request())
        channel.sendall.assert_not_called()

    def test_root_peer_single_request_and_half_close(self):
        channel = self.channel(output=b'{"ok":false}')
        with mock.patch.object(Path, 'lstat', lambda path: self.info(path)), \
                mock.patch.object(bridge.socket, 'socket', return_value=channel):
            self.assertEqual(bridge.LocalDispatcher().perform(request()), {'ok': False})
        channel.connect.assert_called_once_with('/run/ameesh-handoff/local.sock')
        channel.shutdown.assert_called_once_with(socket.SHUT_WR)
        self.assertEqual(json.loads(channel.sendall.call_args.args[0]), request())

    def test_ambiguous_duplicate_and_oversized_receipts_raise(self):
        for output in (b'{"ok":true,"ok":false}', b' ' * 65537, b''):
            channel = self.channel(output=output)
            with mock.patch.object(Path, 'lstat', lambda path: self.info(path)), \
                    mock.patch.object(bridge.socket, 'socket', return_value=channel):
                with self.assertRaises(bridge.BridgeError):
                    bridge.LocalDispatcher().perform(request())


class EnrollmentPolicyTest(unittest.TestCase):
    def test_policy_schema_is_closed_and_uid_is_not_boolean(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.json'
            valid = {'uid': 1001, 'host': 'source', 'agents': ['dev']}
            for value in (valid, dict(valid, uid=True), dict(valid, uid=0),
                          dict(valid, command='id'), dict(valid, agents=['dev', 'dev'])):
                path.write_text(json.dumps(value))
                with mock.patch.object(dispatch, 'protected', return_value=path):
                    if value == valid:
                        self.assertEqual(dispatch.policy(path), valid)
                    else:
                        with self.assertRaises(dispatch.Refused):
                            dispatch.policy(path)

    def test_revocation_reloaded_for_each_connection(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.json'
            rules = {'uid': 1001, 'host': 'source', 'agents': ['dev']}
            with mock.patch.object(dispatch, 'protected', return_value=path):
                path.write_text(json.dumps(rules))
                dispatch.admitted(request(), dispatch.policy(path), 1001)
                path.write_text(json.dumps(dict(rules, agents=['replacement'])))
                with self.assertRaisesRegex(dispatch.Refused, 'agent'):
                    dispatch.admitted(request(), dispatch.policy(path), 1001)

    def test_install_and_rollback_refuse_unprivileged_invocation(self):
        installer = load('installer-local-handoff')
        with mock.patch.object(installer.os, 'geteuid', return_value=1001), \
                mock.patch.object(installer, 'run') as run:
            for action in ('install', 'rollback'):
                with self.assertRaises(ValueError):
                    installer.main([action])
            run.assert_not_called()


class InstallerTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from contextlib import ExitStack
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.installer = load('installer-local-handoff')
        self.module = mock.Mock()
        self.module.protected.side_effect = lambda path, **kw: path
        self.module.policy.return_value = {'uid': 1001, 'host': 'source', 'agents': ['dev']}
        self.module.consistent_policy.side_effect = dispatch.consistent_policy
        self.module.executor.return_value.load_execution.return_value = {
            'host': 'source', 'accounts': {'dev': {}}}
        for name, value in {'BASE': self.root / 'bundle', 'UNITS': self.root / 'units',
                            'RECEIPT': self.root / 'policy/receipt.json',
                            'ENDPOINT': self.root / 'endpoint'}.items():
            self.stack.enter_context(mock.patch.object(self.installer, name, value))
        for path in (self.installer.BASE / 'lib/systemd',
                     self.installer.BASE / 'runtime/bin', self.installer.UNITS,
                     self.installer.RECEIPT.parent, self.installer.ENDPOINT):
            path.mkdir(parents=True, exist_ok=True)
        self.installer.ENDPOINT.chmod(0o700)
        for name in self.installer.NAMES:
            (self.installer.BASE / 'lib/systemd' / name).write_text('unit ' + name)
        (self.installer.BASE / 'runtime/bin/python').write_text('interpreter')
        self.stack.enter_context(mock.patch.object(self.installer, 'bootstrap', return_value=self.module))
        self.stack.enter_context(mock.patch.object(self.installer.os, 'geteuid', return_value=0))
        # Model root metadata without requiring root; preserve actual types/modes.
        original = Path.lstat
        def root_info(path, *args, **kwargs):
            from types import SimpleNamespace
            info = original(path, *args, **kwargs)
            return SimpleNamespace(st_uid=0, st_mode=info.st_mode)
        self.stack.enter_context(mock.patch.object(Path, 'lstat', root_info))
        import pwd
        self.stack.enter_context(mock.patch.object(pwd, 'getpwnam',
            return_value=mock.Mock(pw_uid=1001)))
        self.run = self.stack.enter_context(mock.patch.object(self.installer, 'run'))

    def install(self):
        self.installer.main(['install'])

    def test_install_modes_receipt_and_full_rollback(self):
        import stat
        self.install()
        self.assertEqual(stat.S_IMODE(self.installer.ENDPOINT.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.installer.RECEIPT.stat().st_mode), 0o600)
        for name in self.installer.NAMES:
            self.assertEqual(stat.S_IMODE((self.installer.UNITS / name).stat().st_mode), 0o644)
        self.run.assert_has_calls([mock.call('daemon-reload'),
                                  mock.call('enable', '--now', self.installer.NAMES[0])])
        self.installer.main(['rollback'])
        self.assertFalse(self.installer.RECEIPT.exists())
        self.assertEqual(list(self.installer.UNITS.iterdir()), [])
        self.run.assert_any_call('stop', 'ameesh-handoff-local@*.service')
        self.assertTrue(self.installer.BASE.exists())

    def test_bundle_rejects_pth_fifo_socket_and_symlink_directory(self):
        for kind in ('pth', 'fifo', 'socket', 'directory_link'):
            path = self.installer.BASE / 'runtime' / ('bad.pth' if kind == 'pth' else 'bad')
            channel = None
            try:
                if kind == 'pth':
                    path.write_text('import evil')
                elif kind == 'fifo':
                    os.mkfifo(path)
                elif kind == 'socket':
                    channel = socket.socket(socket.AF_UNIX)
                    channel.bind(str(path))
                else:
                    path.symlink_to(self.installer.BASE / 'lib', target_is_directory=True)
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    self.install()
                self.run.assert_not_called()
            finally:
                if channel:
                    channel.close()
                path.unlink()

    def test_missing_socket_still_stops_instances_and_removes_service(self):
        self.install()
        (self.installer.UNITS / self.installer.NAMES[0]).unlink()
        self.run.reset_mock()
        self.installer.main(['rollback'])
        self.run.assert_has_calls([mock.call('stop', self.installer.NAMES[0], absent_ok=True),
                                   mock.call('stop', 'ameesh-handoff-local@*.service'),
                                   mock.call('daemon-reload')])
        self.assertEqual(list(self.installer.UNITS.iterdir()), [])
        self.assertFalse(self.installer.RECEIPT.exists())

    def test_partial_install_receipt_rolls_back_without_socket(self):
        service = self.installer.NAMES[1]
        path = self.installer.UNITS / service
        path.write_text('partial')
        self.installer.RECEIPT.write_text(json.dumps({'units': {
            service: self.installer.digest(path)}}))
        self.installer.main(['rollback'])
        self.run.assert_any_call('stop', 'ameesh-handoff-local@*.service')
        self.assertFalse(path.exists())

    def test_activation_failure_leaves_receipt_for_rollback(self):
        self.run.side_effect = [None, OSError('systemctl failed')]
        with self.assertRaises(OSError):
            self.install()
        self.assertTrue(self.installer.RECEIPT.exists())
        self.run.side_effect = None
        self.installer.main(['rollback'])
        self.assertEqual(list(self.installer.UNITS.iterdir()), [])

    def test_changed_unit_prevents_removal(self):
        self.install()
        path = self.installer.UNITS / self.installer.NAMES[1]
        path.write_text('operator modified')
        self.run.reset_mock()
        with self.assertRaisesRegex(ValueError, 'unit changed'):
            self.installer.main(['rollback'])
        self.run.assert_not_called()
        self.assertTrue(self.installer.RECEIPT.exists())

    def test_install_refuses_existing_units_and_inconsistent_policy(self):
        path = self.installer.UNITS / self.installer.NAMES[1]
        path.write_text('existing')
        with self.assertRaisesRegex(ValueError, 'existing unit'):
            self.install()
        path.unlink()
        self.module.executor.return_value.load_execution.return_value['host'] = 'other'
        with self.assertRaises(dispatch.Refused):
            self.install()
        self.run.assert_not_called()


class PolicyConsistencyTest(unittest.TestCase):
    def test_host_and_agent_subset(self):
        rules = {'host': 'source', 'agents': ['dev']}
        dispatch.consistent_policy(rules, {'host': 'source', 'accounts': {'dev': {}, 'other': {}}})
        for enrollment in ({'host': 'other', 'accounts': {'dev': {}}},
                           {'host': 'source', 'accounts': {'other': {}}}):
            with self.assertRaises(dispatch.Refused):
                dispatch.consistent_policy(rules, enrollment)

    def test_contract_error_logs_fixed_refused_category(self):
        import io
        module = mock.Mock()
        with mock.patch.object(dispatch.os, 'geteuid', return_value=0), \
                mock.patch.object(dispatch, 'executor', return_value=module), \
                mock.patch.object(dispatch, 'policy', side_effect=dispatch.handoff_contract.ContractError('secret')), \
                mock.patch.object(dispatch.signal, 'signal'), \
                mock.patch.object(dispatch.signal, 'alarm'), \
                mock.patch.object(dispatch.sys, 'stderr', new_callable=io.StringIO) as log:
            module.Uncertain = type('Uncertain', (Exception,), {})
            self.assertEqual(dispatch.main([]), 1)
        self.assertEqual(log.getvalue(), 'local_handoff_refused pid=-1 uid=-1\n')


class SystemctlFailureTest(unittest.TestCase):
    def test_only_missing_unit_exit_is_accepted(self):
        import subprocess
        installer = load('installer-local-handoff')
        for code in (0, 1, 5):
            result = subprocess.CompletedProcess(['systemctl'], code)
            with mock.patch.object(installer.subprocess, 'run', return_value=result):
                if code in (0, 5):
                    installer.run('stop', installer.NAMES[0], absent_ok=True)
                else:
                    with self.assertRaises(subprocess.CalledProcessError):
                        installer.run('stop', installer.NAMES[0], absent_ok=True)
