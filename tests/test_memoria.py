# SPDX-License-Identifier: AGPL-3.0-only
"""External memory uses a real loopback HTTP server, synthetic sources, no DB."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from . import support  # isolate host environment before exercising the CLI
from ameesh import main as command
from ameesh import memoria as m


SOURCE = {"id": "source-a", "channel": "ameesh", "date": "2026-10-21T00:00:00Z",
          "pointer": "ameesh://acme/synthetic/session-a/turn-1"}
REPLY = {"text": "Pat: received a guide (déclaration non corroborée) [source-a]",
         "sources": [SOURCE], "unknown": False, "token_count": 80}
WRITE = {"what": "Pat received a guide; reading is not attested.",
         "channel": "ameesh", "date": "2026-10-21T00:00:00Z", "pointer": SOURCE["pointer"],
         "idempotency_key": "session-a:turn-1", "direction": "out"}
RECEIPT = {"id": "interaction-a", "duplicate": False, "observations": 0,
           "accepted": True, "analysis": "interaction_recorded"}


class MemoriaTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ameesh-memoria-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = "synthetic_token_" + "a" * 32
        self.token_file = self.root / "token"
        self.token_file.write_text(self.token + "\n")
        self.token_file.chmod(0o600)
        self.requests = []
        self.response = REPLY
        self.code = 200
        self.redirect = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.requests.append((self.path, dict(self.headers), json.loads(body)))
                data = (owner.response if isinstance(owner.response, bytes) else
                        json.dumps(owner.response).encode())
                self.send_response(owner.code)
                if owner.redirect:
                    self.send_header("Location", owner.redirect)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.config = self.root / "config.json"
        self.raw_config = {"endpoint": "http://127.0.0.1:%s" % self.server.server_port,
                           "token_file": "token", "project": "acme", "max_tokens": 300}
        self.save_config()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def save_config(self):
        self.config.write_text(json.dumps(self.raw_config))
        self.config.chmod(0o600)

    def client(self):
        return m.Client(m.Settings.load(str(self.config)))

    def test_context_preserves_citations_uncertainty_and_labels(self):
        self.assertEqual(self.client().call("context", {"purpose": "guide"}), REPLY)
        path, headers, body = self.requests[0]
        self.assertEqual(path, "/v1/context")
        self.assertEqual(headers["Authorization"], "Bearer " + self.token)
        self.assertEqual(body, {"purpose": "guide", "audience": "private", "max_tokens": 300,
                                "scope": {"project": "acme"}})

    def test_unknown_is_preserved(self):
        self.response = {"text": "Je ne sais pas.", "sources": [], "unknown": True,
                         "token_count": 24}
        self.assertEqual(self.client().call("context", {"purpose": "absent"}), self.response)

    def test_ask_preserves_question_without_guessing_person(self):
        self.assertEqual(self.client().call("ask", {"question": "What was sent?"}), REPLY)
        self.assertEqual(self.requests[0][0], "/v1/ask")
        self.assertNotIn("people", self.requests[0][2])

    def test_tell_uses_original_source_and_replay_key(self):
        self.response = RECEIPT
        client = self.client()
        self.assertEqual(client.call("tell", WRITE), RECEIPT)
        self.response = {**RECEIPT, "duplicate": True}
        self.assertTrue(client.call("tell", WRITE)["duplicate"])
        self.assertEqual(self.requests[0][2], self.requests[1][2])
        self.assertEqual(self.requests[0][2], {**WRITE, "visibility": "private",
                                             "scope": {"project": "acme"}})

    def test_scope_people_and_visibility_cannot_be_overridden(self):
        for operation, data in (("context", {"purpose": "a"}), ("ask", {"question": "a"}),
                                ("tell", WRITE)):
            for key, value in (("scope", {"project": "other"}), ("people", ["another"]),
                               ("visibility", "public"), ("audience", "public"),
                               ("max_tokens", 4000)):
                with self.subTest(operation=operation, key=key):
                    with self.assertRaisesRegex(m.MemoriaError, "scope_override_refused"):
                        self.client().call(operation, {**data, key: value})
        self.assertEqual(self.requests, [])

    def test_uncited_or_nonidempotent_write_is_refused(self):
        for key in ("what", "channel", "date", "pointer", "idempotency_key"):
            data = dict(WRITE)
            del data[key]
            with self.subTest(key=key), self.assertRaises(m.MemoriaError):
                self.client().call("tell", data)
        self.assertEqual(self.requests, [])

    @unittest.skipUnless(os.name == "posix", "Unix file modes")
    def test_public_config_or_token_is_refused(self):
        for path in (self.config, self.token_file):
            path.chmod(0o644)
            with self.assertRaisesRegex(m.MemoriaError, "private_file_required"):
                self.client().call("context", {"purpose": "a"})
            path.chmod(0o600)
        self.assertEqual(self.requests, [])

    def test_symlink_token_is_refused(self):
        other = self.root / "other-token"
        self.token_file.rename(other)
        self.token_file.symlink_to(other)
        with self.assertRaisesRegex(m.MemoriaError, "private_file_required"):
            self.client().call("context", {"purpose": "a"})

    def test_nonregular_private_file_is_refused(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO unavailable")
        self.token_file.unlink()
        os.mkfifo(self.token_file, 0o600)
        with self.assertRaisesRegex(m.MemoriaError, "private_file_required"):
            self.client().call("context", {"purpose": "a"})

    def test_invalid_endpoint_or_config_is_refused(self):
        for endpoint in ("http://remote.test", "http://user:secret@127.0.0.1", "ftp://localhost",
                         "http://localhost:bad", "http://127.0.0.1?token=secret", "http://[",
                         "http://127.0.0.1\x00", "https://localhost/#fragment"):
            with self.subTest(endpoint=endpoint):
                self.raw_config["endpoint"] = endpoint
                self.save_config()
                with self.assertRaises(m.MemoriaError):
                    self.client()
        self.assertEqual(self.requests, [])

    def test_token_cannot_inject_headers(self):
        for token in ("a" * 40 + "\r\nX-Injected: yes", "short", "é" * 40):
            self.token_file.write_text(token)
            with self.assertRaisesRegex(m.MemoriaError, "invalid_token_file"):
                self.client().call("context", {"purpose": "a"})
        self.assertEqual(self.requests, [])

    def test_redirect_never_forwards_credential(self):
        self.code = 307
        self.redirect = self.raw_config["endpoint"] + "/stolen"
        with self.assertRaisesRegex(m.MemoriaError, "memoria_http_307"):
            self.client().call("context", {"purpose": "a"})
        self.assertEqual(len(self.requests), 1)

    def test_error_body_is_not_printed_and_write_is_not_retried(self):
        self.code = 503
        self.response = {"error": self.token + " PRIVATE_SOURCE_TEXT"}
        with self.assertRaises(m.MemoriaError) as caught:
            self.client().call("tell", WRITE)
        self.assertEqual(str(caught.exception), "memoria_http_503")
        self.assertEqual(len(self.requests), 1)

    def test_inherited_proxy_is_ignored(self):
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1",
                                          "HTTP_PROXY": "http://127.0.0.1:1", "no_proxy": "",
                                          "NO_PROXY": ""}):
            self.assertEqual(self.client().call("context", {"purpose": "a"}), REPLY)

    def test_malformed_or_uncited_answer_is_refused(self):
        for reply in (b"not json", [], {**REPLY, "sources": []},
                      {**REPLY, "unknown": "false"}, {**REPLY, "token_count": 301},
                      {**REPLY, "sources": [{"id": "missing-pointer"}]}):
            self.response = reply
            with self.subTest(reply=reply), self.assertRaises(m.MemoriaError):
                self.client().call("context", {"purpose": "a"})

    def test_large_response_is_refused(self):
        self.response = b"x" * (m.MAX_BYTES + 1)
        with self.assertRaisesRegex(m.MemoriaError, "response_too_large"):
            self.client().call("context", {"purpose": "a"})

    def test_cli_routes_without_connecting_to_mesh_database(self):
        output = io.StringIO()
        with mock.patch("ameesh.db.connect", side_effect=AssertionError("mesh DB used")), \
                contextlib.redirect_stdout(output):
            code = command.main(["memoria", "--config", str(self.config), "context",
                                 "--purpose", "guide"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), REPLY)

    def test_cli_requires_explicit_caller_configuration(self):
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                m.main(["context", "--purpose", "a"])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.requests, [])

    def test_cli_tell_file_and_config_environment(self):
        self.response = RECEIPT
        source = self.root / "source.json"
        source.write_text(json.dumps(WRITE))
        source.chmod(0o600)
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"AMEESH_MEMORIA_CONFIG": str(self.config)}), \
                contextlib.redirect_stdout(output):
            self.assertEqual(m.main(["tell", "--input", str(source)]), 0)
        self.assertEqual(json.loads(output.getvalue()), RECEIPT)

    def test_cli_diagnostic_contains_no_config_path_or_secret(self):
        error_text = io.StringIO()
        with contextlib.redirect_stderr(error_text):
            self.assertEqual(m.main(["--config", str(self.root / "absent-secret"), "context",
                                     "--purpose", "private purpose"]), 1)
        self.assertEqual(error_text.getvalue(), "ameesh memoria: private_file_unavailable\n")
