# SPDX-License-Identifier: AGPL-3.0-only
"""#317: server credential failures, safe device verdict, probe recovery."""
import json
import threading
import types
import unittest
from unittest import mock

from ameesh import db as db_mod, relay, sentinel
from .test_l111_relay import FakeProvider, SERVER_KEY, TOKEN, identity


class RelayCredentialsTest(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()
        self.addCleanup(self.provider.close)
        self.credentials = mock.Mock()
        self.credentials.health.return_value = {}
        self.store = types.SimpleNamespace(credentials=self.credentials, turn_costs=mock.Mock())
        patch = mock.patch("ameesh.storage.of", return_value=self.store)
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = types.SimpleNamespace(host="server-host")
        self.relay = relay.Relay(
            verifier=relay.StaticTokens({TOKEN: identity()}),
            policy=relay.StaticPolicy({"a1": ["deepseek-flash"]}), db=types.SimpleNamespace(query=lambda *_a, **_k: []), cfg=self.cfg,
            secrets=relay.ServerSecrets({"DEEPSEEK_API_KEY": SERVER_KEY}),
            config=relay.RelayConfig(upstreams={"deepseek": self.provider.url}),
            prices={"deepseek-flash": [0.27, 0.07, 1.10]})
        self.relay._check_budget = mock.Mock()
        self.server = relay.make_server(self.relay, "127.0.0.1:0")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%s/api/exec/v1/llm" % self.server.server_address[1]

    def post(self):
        import http.client
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        conn.request("POST", "/api/exec/v1/llm/deepseek/v1/messages",
                     json.dumps({"model": "deepseek-flash", "max_tokens": 20}),
                     {"x-api-key": TOKEN, "Content-Type": "application/json"})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    def failure(self):
        return {"state": "failing", "failing_fingerprint": sentinel._digest(SERVER_KEY),
                "failures": 1, "reason": "upstream authentication refused (HTTP 401)"}

    def test_auth_refusal_records_exact_server_key_and_sanitizes_error(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.provider.error_status = status
                response, body = self.post()
                self.assertEqual(response, 503)
                self.assertEqual(json.loads(body)["error"]["type"], relay.AUTH_BLOCKED)
                args, kwargs = self.credentials.report.call_args
                self.assertEqual(args, ("server-host", "deepseek", "relay", "failing"))
                self.assertEqual(kwargs["fingerprint"], sentinel._digest(SERVER_KEY))
                self.assertEqual(kwargs["source"], "relay")
                self.assertNotIn(SERVER_KEY, str(kwargs))
                self.assertNotIn(b"provider refused", body)
        self.store.turn_costs.insert.assert_not_called()

    def test_blocked_post_and_models_do_not_call_provider_then_resume(self):
        self.credentials.health.return_value = {"relay": self.failure()}
        self.assertEqual(self.post()[0], 503)
        self.assertFalse(relay.credential_ready(self.base, TOKEN, "deepseek"))
        self.assertEqual(self.provider.requests, [])
        self.credentials.health.return_value = {"relay": {"state": "healthy"}}
        self.assertTrue(relay.credential_ready(self.base, TOKEN, "deepseek"))
        self.assertEqual(self.post()[0], 200)

    def test_permission_budget_rate_and_transient_errors_do_not_poison_key(self):
        for status, kind in ((403, "permission_error"), (402, "billing_error"),
                             (429, "rate_limit_error"), (500, "api_error")):
            with self.subTest(status=status):
                self.provider.error_status, self.provider.error_type = status, kind
                self.assertEqual(self.post()[0], status)
        self.credentials.report.assert_not_called()

    def test_unreadable_health_is_closed_and_not_a_credential_verdict(self):
        self.credentials.health.side_effect = db_mod.Unavailable("offline")
        self.assertEqual(self.post()[0], 503)
        with self.assertRaises(db_mod.Unavailable):
            relay.credential_ready(self.base, TOKEN, "deepseek")
        self.assertEqual(self.provider.requests, [])
        self.credentials.report.assert_not_called()

    def probe(self, key="new-key", transport=None, now=None):
        payload = {"is_available": True, "balance_infos": [
            {"currency": "USD", "total_balance": "1"}]}
        return relay.probe_credentials(
            self.cfg, self.relay.db, environ={"DEEPSEEK_API_KEY": key},
            transport=transport or (lambda *args: (payload, "")), now=now)

    def test_probe_requires_positive_authenticated_response(self):
        self.credentials.health.return_value = {"relay": self.failure()}
        self.credentials.report.return_value = {"state": "healthy"}
        transport = mock.Mock(return_value=(None, "HTTPError"))
        self.assertEqual(self.probe(SERVER_KEY, transport)[0]["state"], "failing")
        transport.assert_called_once()
        self.assertEqual(self.probe(transport=transport)[0]["state"], "failing")
        self.credentials.report.assert_not_called()
        self.assertEqual(self.probe()[0]["state"], "healthy")
        args, kwargs = self.credentials.report.call_args
        self.assertEqual(args, ("server-host", "deepseek", "relay", "healthy"))
        self.assertEqual(kwargs["expect_failures"], 1)

    def test_transient_401_recovers_same_key_after_backoff(self):
        health = {}
        self.credentials.health.side_effect = lambda *args: health

        def report(host, harness, account, state, **kwargs):
            if state == "failing":
                health[account] = self.failure() | {"last_failure_ts": 1000}
            else:
                self.assertEqual(kwargs["expect_failures"], 1)
                health[account] = {"state": "healthy"}
            return {"state": state}

        self.credentials.report.side_effect = report
        self.provider.error_status = 401
        self.assertEqual(self.post()[0], 503)
        self.provider.error_status = 0
        transport = mock.Mock(return_value=({"is_available": True, "balance_infos": [
            {"currency": "USD", "total_balance": "1"}]}, ""))
        self.assertEqual(self.probe(SERVER_KEY, transport, now=1599)[0]["state"], "failing")
        transport.assert_not_called()
        self.assertEqual(self.post()[0], 503)
        self.assertEqual(self.probe(SERVER_KEY, transport, now=1600)[0]["state"], "healthy")
        transport.assert_called_once()
        self.assertEqual(transport.call_args.args[1]["Authorization"], "Bearer " + SERVER_KEY)
        self.assertTrue(relay.credential_ready(self.base, TOKEN, "deepseek"))
        self.assertEqual(self.post()[0], 200)

    def test_rotated_key_bypasses_recent_failure_backoff(self):
        self.credentials.health.return_value = {"relay": self.failure() | {"last_failure_ts": 1000}}
        self.credentials.report.return_value = {"state": "healthy"}
        self.assertEqual(self.probe(now=1001)[0]["state"], "healthy")

    def test_failure_during_probe_wins_and_key_replacement_is_not_accepted(self):
        self.credentials.health.return_value = {"relay": self.failure()}
        self.credentials.report.return_value = {"skipped": True}
        self.assertEqual(self.probe(SERVER_KEY)[0]["state"], "failing")
        self.assertEqual(self.credentials.report.call_args.kwargs["expect_failures"], 1)
        self.credentials.report.reset_mock()
        with mock.patch.object(relay.ServerSecrets, "key", side_effect=["key-a", "key-b"]):
            self.assertEqual(self.probe()[0]["state"], "failing")
        self.credentials.report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
