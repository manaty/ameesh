# SPDX-License-Identifier: AGPL-3.0-only
"""Role notification grants survive the real process boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest import mock

from ameesh import role_run
from ameesh.platform import child_environment as E


class RoleNotifyCredentialsTest(unittest.TestCase):
    def test_grants_are_scoped_and_file_sources_take_precedence(self):
        raw = {"channels": {
            "slack": {},
            "push": {"type": "ntfy", "token_env": "CUSTOM_VALUE"},
            "file": {"token_env": "FILE_SECRET", "token_file": "/unused"},
        }}
        source = dict(CUSTOM_VALUE="push", AMEESH_SLACK_WEBHOOK="slack",
                      FILE_SECRET="file", OPENAI_API_KEY="provider", GH_TOKEN="github")
        with mock.patch.object(E, "DECLARED_CREDENTIALS", set()):
            granted = E.role_transport(source, ["/opt/bin/ameesh", "notify", "--once"], raw)
            clean = E.for_child([sys.executable], granted)
            self.assertEqual(set(clean), {"CUSTOM_VALUE", "AMEESH_SLACK_WEBHOOK", "GH_TOKEN"})
            for command in (["ameesh", "release"], ["sh", "-c", "ameesh notify"],
                            ["python3", "notify"]):
                self.assertEqual(set(E.for_child(command, E.role_transport(source, command, raw))),
                                 {"GH_TOKEN"})
            self.assertEqual(E.for_child([sys.executable], source), {})
            self.assertEqual(E.for_child([sys.executable], dict(clean)), {})

    def test_implicit_slack_route_preserves_default_webhook(self):
        with mock.patch.object(E, "DECLARED_CREDENTIALS", set()):
            for raw in ({"default": ["slack"]},
                        {"routes": {"human:test": ["slack"]}}):
                self.assertEqual(E.notify_credentials(raw), {"AMEESH_SLACK_WEBHOOK"})

    def test_role_child_sends_signed_primary_request_with_filter_active(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, dict(self.headers),
                                 self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = 'http://127.0.0.1:%s' % server.server_port
        raw = {'channels': {'primary': {'type': 'nexlink', 'url': url,
                                       'token_env': 'CUSTOM_VALUE'}}}
        script = '''
import os,subprocess,sys
from ameesh import notify
from ameesh.platform import child_environment as E
E.install()
assert 'OPENAI_API_KEY' not in os.environ
assert os.environ['CUSTOM_VALUE'] == 'synthetic-secret'
E.notify_credentials({'channels': {'primary': {'token_env': 'CUSTOM_VALUE'}}})
subprocess.run([sys.executable, '-c', "import os; assert 'CUSTOM_VALUE' not in os.environ"], check=True)
channel = notify.Channel('primary', 'nexlink', {'url': %r, 'token_env': 'CUSTOM_VALUE'})
notify.Sender()._nexlink_call(channel, 'POST', '/mesh/alerts', {'title': 'role alert'}, 5)
''' % url
        try:
            with tempfile.TemporaryDirectory() as root:
                command = Path(root, 'ameesh')
                command.write_text('#!%s\n%s' % (sys.executable, script))
                command.chmod(0o700)
                cfg = SimpleNamespace(host='test-host', notify=raw)
                args = Namespace(role='test-role', command=[str(command), 'notify', '--once'],
                                 min_left=0, dry_run=False, json=False)
                report = dict(role='test-role', host='test-host', stopped=False, held=True,
                              epoch=1, owner='test', left_s=100)
                original = subprocess.Popen.__init__
                with mock.patch.dict(os.environ, {
                        'CUSTOM_VALUE': 'synthetic-secret', 'OPENAI_API_KEY': 'provider',
                        'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}), \
                        mock.patch.object(E, 'DECLARED_CREDENTIALS', set()), \
                        mock.patch.object(role_run.db_mod, 'connect'), \
                        mock.patch.object(role_run, 'live', return_value=report):
                    try:
                        E.install()
                        self.assertEqual(role_run.run(cfg, args), 0)
                    finally:
                        subprocess.Popen.__init__ = original
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][0], '/mesh/alerts')
            self.assertEqual(json.loads(requests[0][2]), {'title': 'role alert'})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
