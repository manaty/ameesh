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
