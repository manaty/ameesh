# SPDX-License-Identifier: AGPL-3.0-only
"""L87 (#375): the human actions of `ameesh serve` — POST, `Idempotency-Key`,
the identity of the API (never the server's environment), and no receipt.

Without a database: the guards of a POST (token, key, body, methods) and the
static proof that no route reaches a function that produces or attaches a
receipt. On a migrated schema: each action through HTTP, its effect, its
trace, and the replay of a key.
"""
from __future__ import annotations

import ast
import dataclasses
import http.client
import json
import unittest
from pathlib import Path

from ameesh import budget, fil, registry, serve, serve_actions, storage, work
from ameesh import resources as res

from .support import PgTestCase
from .test_l84_serve import TOKEN, ServerMixin

ROOT = Path(__file__).resolve().parent.parent
KEY = "key-0000000001"


class PostMixin(ServerMixin):
    def post(self, path, body=None, *, key=KEY, raw=None, ctype="application/json",
             auth=True, headers=None):
        payload = raw if raw is not None else json.dumps(body or {}).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", path, skip_host=True)
            conn.putheader("Host", "127.0.0.1:%d" % self.port)
            if auth:
                conn.putheader("Authorization", "Bearer %s" % TOKEN)
            if key is not None:
                conn.putheader("Idempotency-Key", key)
            if ctype:
                conn.putheader("Content-Type", ctype)
            conn.putheader("Content-Length", str(len(payload)))
            for name, value in (headers or {}).items():
                conn.putheader(name, value)
            conn.endheaders()
            conn.send(payload)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), json.loads(data) if data else None
        finally:
            conn.close()


class GuardsTest(PostMixin, unittest.TestCase):
    """No database: a POST refused before any action, nothing written."""

    def setUp(self):
        self.writes = []
        ctx = serve.Context(cfg=object(), human="human:owner")
        self.start(ctx, read=lambda fn: self.fail("no read"),
                   write=lambda fn: self.writes.append(fn) or (200, {}, False))

    def test_token_required(self):
        status, headers, body = self.post("/api/v1/agents/a1/messages", {"text": "x"},
                                          auth=False)
        self.assertEqual((status, body["error"]), (401, "unauthorized"))
        self.assertEqual(self.writes, [])

    def test_one_idempotency_key_required(self):
        for key in (None, "short", "a" * 129, "has space1"):
            status, _h, body = self.post("/api/v1/agents/a1/messages", {"text": "x"}, key=key)
            self.assertEqual((status, body["error"]), (400, "idempotency_key"), key)
        status, _h, body = self.post("/api/v1/agents/a1/messages", {"text": "x"},
                                     headers={"Idempotency-Key": "key-0000000002"})
        self.assertEqual((status, body["error"]), (400, "idempotency_key"))
        self.assertEqual(self.writes, [])

    def test_body_is_a_bounded_json_object_of_known_fields(self):
        path = "/api/v1/agents/a1/messages"
        cases = (
            (dict(raw=b'{"text": "x"}', ctype="text/plain"), 415, "unsupported_media_type"),
            (dict(raw=b"x" * (serve_actions.MAX_BODY + 1)), 413, "too_large"),
            (dict(raw=b"{not json"), 400, "bad_body"),
            (dict(raw=b"[1]"), 400, "bad_body"),
            (dict(raw=b'{"text": "a", "text": "b"}'), 400, "bad_body"),
            (dict(raw=b'{"text": "x", "urgent": NaN}'), 400, "bad_body"),
            (dict(body={"text": "x", "sender": "human:other"}), 400, "bad_body"),
            (dict(body={}), 400, "bad_body"),
        )
        for kwargs, status, code in cases:
            got, _h, body = self.post(path, **kwargs)
            self.assertEqual((got, body["error"]), (status, code), kwargs)
        self.assertEqual(self.writes, [])

    def test_methods_follow_the_route(self):
        status, headers, _b = self.get("/api/v1/agents/a1/stop")
        self.assertEqual((status, headers.get("Allow")), (405, "POST"))
        status, headers, _b = self.post("/api/v1/agents", {})
        self.assertEqual((status, headers.get("Allow")), (405, "GET, HEAD"))
        status, _h, body = self.post("/api/v1/nothing", {})
        self.assertEqual((status, body["error"]), (404, "not_found"))
        status, _h, body = self.post("/api/v1/agents/a1/messages?x=1", {"text": "x"})
        self.assertEqual((status, body["error"]), (400, "bad_query"))

    def test_without_a_writer_actions_are_unavailable(self):
        self._stop()
        self.start(serve.Context(cfg=object(), human="human:owner"),
                   read=lambda fn: None)
        status, _h, body = self.post("/api/v1/agents/a1/messages", {"text": "x"})
        self.assertEqual((status, body["error"]), (503, "unavailable"))


class NoReceiptTest(unittest.TestCase):
    """Study §2.1: the API has no approval route — no route can produce or
    attach a receipt. Checked on the source of the routes and on the paths."""

    #: functions that approve, produce, verify-and-consume or attach a receipt,
    #: by module (a call is `module.function` in these modules' style)
    FORBIDDEN = {
        "actions": {"approve", "retry", "replace", "_authorize", "_bind"},
        "receipts": {"verify_receipt", "consume_nonce", "register_standing",
                     "standing_reserve"},
        "actions_cli": {"_attach", "cmd_fetch_receipt", "cmd_approve", "cmd_replace"},
        "approve_client": {"fetch_receipt"},
        "store": {"save_receipt_once"},
        "authority": {"create_approval", "build_approval", "consume_approval",
                      "find_approval"},
        "decisions": {"_sign"},
        "mesh_cli": {"cmd_approve"},
    }

    def forbidden_calls(self, tree) -> set:
        found = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.attr in self.FORBIDDEN.get(node.func.value.id, ())):
                found.add("%s.%s" % (node.func.value.id, node.func.attr))
            # a forbidden module imported by name would hide its calls
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in self.FORBIDDEN.get((node.module or "").split(".")[-1],
                                                        ()):
                        found.add("import %s" % alias.name)
        return found

    def test_no_route_reaches_a_receipt_function(self):
        for name in ("serve.py", "serve_actions.py"):
            with self.subTest(module=name):
                tree = ast.parse((ROOT / "src" / "ameesh" / name).read_text(encoding="utf-8"))
                self.assertEqual(self.forbidden_calls(tree), set())

    def test_the_approval_route_files_and_returns_a_link_only(self):
        source = (ROOT / "src" / "ameesh" / "actions_cli.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "request_approval")
        self.assertEqual(self.forbidden_calls(fn), set())
        called = {n.func.attr for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertIn("create_request", called)

    def test_no_path_approves_or_receives(self):
        for action in serve.ACTIONS:
            path = action.path.lower()
            self.assertNotIn("receipt", path)
            self.assertFalse(path.endswith("/approve"), path)


class ActionsPgTest(PostMixin, PgTestCase):
    """On a migrated schema: each action, its effect and its trace."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE api_idempotency, budget_limits, budget_events, "
                        "host_lifecycle CASCADE")
        registry.upsert(self.db, "a1", chantier="demo", harness="codex", host="h1")
        storage.of(self.db).hosts.record(res.sample("h1"))
        self.lot = work.add(self.db, title="lot servi", app="demo", assignee="a1")
        fil.transport_for(self.cfg).post(fil.ThreadRef("demo"), fil.Entry(
            author="agent:a1", recipient="tous", text="fil du projet"))
        cfg = dataclasses.replace(self.cfg, read_only=True,
                                  interrupt_senders=("allowed",))
        self.start(serve.Context(cfg=cfg, human="human:owner"),
                   serve.db_reader(self.cfg), write=serve.db_writer(self.cfg))

    def mailbox(self):
        return self.db.query("select sender, recipient, body, payload::text as payload,"
                             " work_item_id from agent_mailbox order by id")

    def test_message_is_the_humans_and_replays_once(self):
        body = {"text": "bonjour", "lot": self.lot["id"]}
        status, headers, out = self.post("/api/v1/agents/a1/messages", body)
        self.assertEqual(status, 200, out)
        self.assertEqual((out["schema"], out["action"], out["by"], out["for"]),
                         ("ameesh-action/1", "message", "human:owner", "human:owner"))
        rows = self.mailbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["sender"], rows[0]["recipient"], rows[0]["work_item_id"]),
                         ("owner", "a1", str(self.lot["id"])))
        self.assertTrue(json.loads(rows[0]["payload"])["human"])
        self.assertNotIn("Idempotent-Replayed", headers)
        # the same key and request: the first answer, nothing sent twice
        again, headers, out2 = self.post("/api/v1/agents/a1/messages", body)
        self.assertEqual((again, out2, headers.get("Idempotent-Replayed")),
                         (200, out, "true"))
        self.assertEqual(len(self.mailbox()), 1)
        # the same key for another request: refused, nothing sent
        other, _h, err = self.post("/api/v1/agents/a1/messages", {"text": "autre"})
        self.assertEqual((other, err["error"]), (422, "idempotency_mismatch"))
        self.assertEqual(len(self.mailbox()), 1)
        # the lot only labels the thread: it is never reassigned
        self.assertEqual(work.get(self.db, self.lot["id"])["assignee"], "a1")

    def test_refusals_are_stored_and_nothing_is_sent(self):
        status, _h, out = self.post("/api/v1/agents/ghost/messages", {"text": "x"})
        self.assertEqual((status, out["error"]), (404, "not_found"))
        again, headers, out2 = self.post("/api/v1/agents/ghost/messages", {"text": "x"})
        self.assertEqual((again, out2, headers.get("Idempotent-Replayed")), (404, out, "true"))
        status, _h, out = self.post("/api/v1/agents/a1/messages",
                                    {"text": "x", "lot": 999999}, key="key-0000000003")
        self.assertEqual((status, out["error"]), (404, "not_found"))
        self.assertEqual(self.mailbox(), [])

    def test_a_key_still_running_is_busy(self):
        storage.of(self.db).api_requests.reserve(
            "human:owner", "key-0000000004",
            serve.fingerprint("/api/v1/agents/a1/messages", {"text": "x"}), retention_s=86400)
        status, _h, out = self.post("/api/v1/agents/a1/messages", {"text": "x"},
                                    key="key-0000000004")
        self.assertEqual((status, out["error"]), (409, "idempotency_in_progress"))
        self.assertEqual(self.mailbox(), [])

    def test_keys_are_per_human(self):
        storage.of(self.db).api_requests.reserve("human:other", KEY, "0" * 64,
                                                 retention_s=86400)
        status, _h, _out = self.post("/api/v1/agents/a1/messages", {"text": "x"})
        self.assertEqual(status, 200)

    def test_a_transient_failure_releases_the_key(self):
        def unavailable(ctx, db, params, body):
            raise serve_actions.ActionError(503, "thread_unavailable", "down")
        action = serve_actions.Action("/x", "x", "x", unavailable, {})
        ctx = serve.Context(cfg=self.cfg, human="human:owner")
        status, _data, _r = serve.act(ctx, self.db, action, {}, {}, "key-0000000005", "1" * 64)
        self.assertEqual(status, 503)
        self.assertIsNone(storage.of(self.db).api_requests.reserve(
            "human:owner", "key-0000000005", "1" * 64, retention_s=86400))

    def test_publish_in_a_thread(self):
        status, _h, out = self.post("/api/v1/threads/demo", {"text": "note de l'humain"})
        self.assertEqual(status, 200, out)
        entries = fil.transport_for(self.cfg).read(fil.ThreadRef("demo"))
        self.assertEqual((entries[-1].author, entries[-1].text),
                         ("human:owner", "note de l'humain"))
        status, _h, out = self.post("/api/v1/threads/autre/%d" % self.lot["id"],
                                    {"text": "x"}, key="key-0000000006")
        self.assertEqual((status, out["error"]), (404, "not_found"))

    def test_interrupt_needs_the_senders_list(self):
        status, _h, out = self.post("/api/v1/agents/a1/interrupt", {"text": "stop"})
        self.assertEqual((status, out["error"]), (403, "forbidden"))
        self.assertEqual(self.mailbox(), [])

    def test_stop_with_a_reason_traced(self):
        status, _h, out = self.post("/api/v1/agents/a1/stop", {"reason": "boucle"})
        self.assertEqual(status, 200, out)
        row = registry.get(self.db, "a1")
        self.assertEqual((row["status"], row["stop_reason"]), ("stopped", "manuel"))
        self.assertIn("human:owner", row["status_text"])
        self.assertIn("boucle", row["status_text"])
        status, _h, out = self.post("/api/v1/agents/a1/stop", {"reason": "boucle"},
                                    key="key-0000000007")
        self.assertEqual((status, out["result"]["changed"]), (200, False))
        status, _h, out = self.post("/api/v1/agents/ghost/stop", {"reason": "x"},
                                    key="key-0000000008")
        self.assertEqual(status, 404)

    def test_assign_to_a_human_never_external(self):
        status, _h, out = self.post("/api/v1/lots/%d/assign" % self.lot["id"],
                                    {"assignee": "human:bob"})
        self.assertEqual(status, 200, out)
        self.assertEqual(work.get(self.db, self.lot["id"])["assignee"], "human:bob")
        events = self.db.query("select actor from work_item_events where work_item_id = %s"
                               " order by id desc limit 1", (self.lot["id"],))
        self.assertEqual(events[0]["actor"], "human:owner")
        status, _h, out = self.post("/api/v1/lots/%d/assign" % self.lot["id"],
                                    {"assignee": "a1", "externe": True}, key="key-0000000009")
        self.assertEqual((status, out["error"]), (400, "bad_body"))

    def test_drain_under_the_humans_name(self):
        status, _h, out = self.post("/api/v1/hosts/h1/drain", {"duration_s": 3600})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["result"]["actor"], "human:owner")

    def test_a_cap_is_only_lowered(self):
        budget.change(self.cfg, self.db, agent="a1", window=3600, usd=10.0, actor="human:x")
        status, _h, out = self.post("/api/v1/budget/caps",
                                    {"window": "hour", "usd": 5, "agent": "a1"})
        self.assertEqual((status, out["result"]["previous"]), (200, 10.0), out)
        for usd, key in ((5, "key-0000000010"), (20, "key-0000000011")):
            status, _h, out = self.post("/api/v1/budget/caps",
                                        {"window": "hour", "usd": usd, "agent": "a1"}, key=key)
            self.assertEqual((status, out["error"]), (409, "not_a_lowering"), usd)
        limits = {(r["scope"], r["window_s"]): r["usd"]
                  for r in storage.of(self.db).budgets.limits()}
        self.assertEqual(limits[("a1", 3600)], 5.0)

    def test_approval_request_of_an_unknown_action(self):
        status, _h, out = self.post("/api/v1/actions/act_%s/approval-request" % ("0" * 26),
                                    {})
        self.assertEqual((status, out["error"]), (404, "not_found"))


if __name__ == "__main__":
    unittest.main()
