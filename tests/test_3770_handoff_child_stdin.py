# SPDX-License-Identifier: AGPL-3.0-only
"""Unprivileged socket-activation regression: children cannot forge receipts."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import unittest

from .test_3509_local_handoff import peercred, request


WORKER = r'''
import os, socket, subprocess, sys, types
from unittest import mock
from tests.test_3509_local_handoff import dispatch, load
companion = load('hote-handoff')
attack = "import os; os.write(0, b'forged-receipt')"
def execute(value):
    argv = [sys.executable, '-c', attack]
    if sys.argv[1] == 'dispatcher':
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    else:
        result = companion._bounded_run(argv, timeout=5, env=os.environ.copy(), check=False)
    # DEVNULL may be opened read/write; writing can succeed harmlessly.
    assert result.returncode in (0, 1), result
    return {'host': 'source', 'action_id': value['action_id'],
            'controller_epoch': 3, 'ok': False, 'reason': 'identity'}
rules = {'uid': os.getuid(), 'host': 'source', 'agents': ['dev']}
if sys.argv[1] == 'dispatcher':
    module = types.SimpleNamespace(load_execution=lambda: {}, execute=execute,
                                   Uncertain=companion.Uncertain)
    with mock.patch.object(dispatch.os, 'geteuid', return_value=0), \
         mock.patch.object(dispatch, 'executor', return_value=module), \
         mock.patch.object(dispatch, 'policy', return_value=rules), \
         mock.patch.object(dispatch, 'consistent_policy'):
        sys.exit(dispatch.main([]))
else:
    with socket.socket(fileno=os.dup(0)) as channel:
        dispatch.serve(channel, rules, execute)
'''


@peercred
class ChildStdinTest(unittest.TestCase):
    def test_each_boundary_prevents_child_receipt_injection(self):
        for boundary in ('dispatcher', 'bounded_run'):
            with self.subTest(boundary=boundary):
                self.socket_exchange(boundary)

    def socket_exchange(self, boundary):
        client, server = socket.socketpair()
        with client, server:
            client.settimeout(10)
            with subprocess.Popen([sys.executable, '-c', WORKER, boundary],
                    cwd=Path(__file__).resolve().parents[1], stdin=server,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE) as worker:
                server.close()
                client.sendall(json.dumps(request()).encode())
                client.shutdown(socket.SHUT_WR)
                chunks = []
                while chunk := client.recv(65536):
                    chunks.append(chunk)
                stdout, stderr = worker.communicate(timeout=10)
                self.assertEqual(worker.returncode, 0, (stdout, stderr))
                receipt = json.loads(b''.join(chunks))
                self.assertIs(receipt['ok'], False)
                self.assertEqual(receipt['reason'], 'identity')
