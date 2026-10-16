# SPDX-License-Identifier: AGPL-3.0-only
"""#1349: responsible-reader, lease, redaction and cross-host observations."""
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ameesh import adapters, agent_peek as peek, config, decisions, registry, serve, storage, work
from tests.support import PgTestCase
from tests.test_l84_serve import ServerMixin


class SelectionTest(unittest.TestCase):
    def test_empty_workspace_never_reports_the_readers_repository(self):
        self.assertFalse(peek.workspace("")["available"])
        self.assertEqual(peek.clean("hello\x1b]2;title\x07\x00"), "hello ]2;title")

    def test_protocol_selection_excludes_thoughts_arguments_outputs_and_envelopes(self):
        events = [
            {"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE THOUGHT"}},
            {"type": "item.completed", "item": {"type": "command_execution", "command":
                "KEY=PRIVATE_ARG curl https://example/PRIVATE_ARG", "aggregated_output": "PRIVATE OUTPUT"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text":
                "Checks passed; OPAQUE=PRIVATE_ENV api_key=PRIVATE_TOKEN"}},
            {"type": "user", "message": {"content": "PRIVATE PROMPT"}},
            {"type": "assistant", "parent_tool_use_id": "child", "message":
                {"content": [{"type": "text", "text": "PRIVATE CHILD"}]}},
        ]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            path.write_text("broken\n" + "\n".join(json.dumps(e) for e in events) + "\n")
            data = peek.selected_events(path, adapters.CodexStream())
            self.assertEqual(len(data), 2)
            self.assertEqual(data[0]["summary"], "curl")
            self.assertIn("Checks passed", data[1]["summary"])
            self.assertNotIn("PRIVATE", json.dumps(data))
            self.assertEqual(peek.selected_events(path, None), [])

    def test_command_summary_uses_only_finite_vocabulary(self):
        for command, expected in (("/bin/bash -lc 'git status --password PRIVATE'", "git status"),
                                  ("OPAQUE=PRIVATE pytest tests/test_PRIVATE.py", "pytest"),
                                  ("/bin/PRIVATE PRIVATE", "command_execution"),
                                  ("git PRIVATE", "git")):
            self.assertEqual(peek.command_label({"item": {"command": command}}, "command_execution"), expected)

    def test_redact_before_truncation_and_whitelist_payload(self):
        values = ['TOKEN="' + "PRIVATE " * 100 + '"', "postgresql://user:PRIVATE@db/x",
                  "OPERATOR_NOTE='PRIVATE VALUE'", "Bearer PRIVATE", "ghp_" + "a" * 40,
                  "eyJabc.def.ghi", "-----BEGIN PRIVATE KEY-----\nPRIVATE\n-----END PRIVATE KEY-----"]
        for value in values:
            clean = peek.clean(value, 20)
            self.assertNotIn("PRIVATE", clean)
            self.assertIn("redacted", peek.clean(value))
        clean = peek.sanitise({"raw": "PRIVATE", "events": [
            {"kind": "thought", "summary": "PRIVATE"},
            {"kind": "tool", "summary": "Bash", "arguments": "PRIVATE"}],
            "workspace": {"branch": "main", "path": "PRIVATE"}})
        self.assertNotIn("PRIVATE", json.dumps(clean))
        self.assertEqual(peek.sanitise({"events": {"raw": "PRIVATE"}})["events"], [])

    def test_tail_and_item_bounds_survive_large_and_truncated_records(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            event = lambda t: json.dumps({"type": "item.completed", "item":
                          {"type": "agent_message", "text": t}})
            path.write_text(event("x" * peek.TAIL_BYTES) + "\n" +
                            "\n".join(event(str(i)) for i in range(60)) + "\n{" )
            result = peek.selected_events(path, adapters.CodexStream(), limit=3)
            self.assertEqual([r["summary"] for r in result], ["57", "58", "59"])

    def test_workspace_includes_staged_and_unstaged_changes_without_external_diff(self):
        with tempfile.TemporaryDirectory() as root:
            def git(*args):
                subprocess.run(["git", "-C", root, *args], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            git("init", "-b", "work")
            path = Path(root) / "tracked.txt"
            path.write_text("before\n")
            git("add", "tracked.txt")
            git("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "base")
            path.write_text("after\n")
            git("add", "tracked.txt")
            path.write_text("after\nunstaged\n")
            git("config", "diff.external", "false")
            data = peek.workspace(root)
            self.assertTrue(data["available"])
            self.assertEqual(data["branch"], "work")
            self.assertIn("tracked.txt", data["diff_stat"])
            self.assertIn("2 insertions", data["diff_stat"])

    def test_cli_denies_agent_context_without_exporting_a_snapshot(self):
        with mock.patch("ameesh.config.load"), mock.patch("ameesh.db.connect") as connect, \
                mock.patch.object(decisions, "human_session", side_effect=decisions.DecisionError("agent context")), \
                mock.patch.object(peek, "snapshot") as snapshot, \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            self.assertEqual(peek.main(["worker", "--json"]), 2)
            self.assertEqual(out.getvalue(), "")
            snapshot.assert_not_called()
            connect.return_value.close.assert_called_once()


class DatabaseTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "peek-worker", harness="codex", host="remote-host", mode="execute")
        self.db.execute("UPDATE agent_registry SET responsible='human:owner', lease_owner='runner',"
                        " lease_epoch=7, lease_expires_at=now()+interval '5 minutes'"
                        " WHERE name='peek-worker'")
        self.store = storage.of(self.db).operations
        self.payload = {"workspace": {"branch": "work", "diff_stat": "tracked.txt | 2 ++",
                                      "available": True},
                        "events": [{"kind": "message", "summary": "Tests passed TOKEN=PRIVATE"}]}

    def test_snapshot_cross_host_responsibility_and_missing_producer(self):
        initial = peek.snapshot(self.db, "peek-worker", human="human:owner")
        self.assertTrue(initial["stale"])
        self.assertIsNone(initial["captured_ts"])
        self.assertEqual(initial["host"], "remote-host")
        self.assertTrue(self.store.publish_peek("peek-worker", "runner", 7, self.payload))
        for human in ("human:stranger", "peek-worker", ""):
            with self.assertRaises(PermissionError):
                peek.snapshot(self.db, "peek-worker", human=human)
        item = work.add(self.db, title="current work", assignee="peek-worker", actor="peek-worker")
        work.move(self.db, item["id"], "build", actor="peek-worker")
        data = peek.snapshot(self.db, "peek-worker", human="human:owner")
        self.assertEqual(data["lot"]["id"], item["id"])
        self.assertEqual(data["workspace"]["branch"], "work")
        self.assertFalse(data["stale"])
        self.assertNotIn("PRIVATE", json.dumps(data))
        self.assertNotIn("PRIVATE", self.db.query("SELECT payload FROM agent_peek")[0]["payload"]["events"][0]["summary"])
        self.assertTrue(peek.snapshot(self.db, "peek-worker", human="human:owner",
                                     now=data["captured_ts"]+46)["stale"])

    def test_lease_fencing_and_old_host_or_epoch_never_reused(self):
        self.assertTrue(self.store.publish_peek("peek-worker", "runner", 7, self.payload))
        for owner, epoch in (("other", 7), ("runner", 6)):
            self.assertFalse(self.store.publish_peek("peek-worker", owner, epoch, {}))
        self.db.execute("UPDATE agent_registry SET lease_expires_at=now()-interval '1 second'"
                        " WHERE name='peek-worker'")
        self.assertFalse(self.store.publish_peek("peek-worker", "runner", 7, {}))
        self.db.execute("UPDATE agent_registry SET host='next-host', lease_epoch=8"
                        " WHERE name='peek-worker'")
        data = peek.snapshot(self.db, "peek-worker", human="human:owner")
        self.assertTrue(data["stale"])
        self.assertIsNone(data["captured_ts"])
        self.assertEqual(data["events"], [])
        self.assertEqual(self.db.query("SELECT lease_epoch FROM agent_peek")[0]["lease_epoch"], 7)

    def test_publisher_produces_cross_host_snapshot_and_stops(self):
        Path(self.cfg.agent_dir("peek-worker")).mkdir(parents=True, exist_ok=True)
        Path(self.cfg.agent_dir("peek-worker"), "events.jsonl").write_text(json.dumps({
            "type": "item.completed", "item": {"type": "agent_message", "text": "Ready TOKEN=PRIVATE"}})+"\n")
        publisher = peek.Publisher(self.cfg, agent="peek-worker", owner="runner", epoch=7,
                                   cwd=lambda: self.cfg.state_dir,
                                   reader=lambda: adapters.CodexStream())
        publisher.close()
        publisher.thread.join(timeout=10)
        self.assertFalse(publisher.thread.is_alive())
        data = peek.snapshot(self.db, "peek-worker", human="human:owner")
        self.assertIsNotNone(data["captured_ts"])
        self.assertIn("Ready", data["events"][0]["summary"])
        self.assertIn("redacted", data["events"][0]["summary"])
        self.assertNotIn("PRIVATE", json.dumps(data))
        self.assertEqual(data["host"], "remote-host")


class ApiTest(ServerMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="peek-api-")
        self.addCleanup(__import__('shutil').rmtree, self.tmp)
        self.db = mock.Mock()
        self.start(serve.Context(cfg=config.Config(), human="human:owner"),
                   lambda fn: fn(self.db), rate_per_minute=1000)

    def test_http_requires_token_and_responsible_human(self):
        with mock.patch.object(peek, "snapshot", side_effect=PermissionError) as build:
            self.assertEqual(self.get_json("/api/v1/agents/worker/peek", auth=False)[0], 401)
            build.assert_not_called()
            status, data = self.get_json("/api/v1/agents/worker/peek")
            self.assertEqual(status, 403)
            self.assertEqual(data["error"], "forbidden")
            build.assert_called_once_with(self.db, "worker", human="human:owner", limit=10)

    def test_http_and_cli_share_snapshot_limits(self):
        with mock.patch.object(peek, "snapshot", return_value={"schema": peek.SCHEMA}) as build:
            self.assertEqual(self.get_json("/api/v1/agents/worker/peek?limit=3")[0], 200)
            build.assert_called_once_with(self.db, "worker", human="human:owner", limit=3)
        for limit in ("bad", "0", "51"):
            self.assertEqual(self.get_json("/api/v1/agents/worker/peek?limit="+limit)[0], 400)

    def test_http_cli_uses_server_identity_without_database_or_session_binding(self):
        token_path = Path(self.tmp) / "token"
        from tests.test_l84_serve import TOKEN
        token_path.write_text(TOKEN)
        token_path.chmod(0o600)
        data = {"schema": peek.SCHEMA, "agent": "worker", "host": "remote-host"}
        with mock.patch.object(peek, "snapshot", return_value=data) as build, \
                mock.patch.object(decisions, "agent_in_ancestry", return_value=None), \
                mock.patch("ameesh.db.connect") as connect, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(peek.main(["worker", "--json", "--limit", "3", "--url",
                "http://127.0.0.1:%d" % self.port, "--token-file", str(token_path)]), 0)
            self.assertEqual(json.loads(out.getvalue()), data)
            build.assert_called_once_with(self.db, "worker", human="human:owner", limit=3)
            connect.assert_not_called()
        for url in ("http://example.org", "https://user:secret@example.org", "file:///tmp/token"):
            with self.assertRaises(ValueError):
                peek.read_api(url, str(token_path), "worker", 3)


from tests.test_l108_executor_server import ServeurExecTest, OWNER


class ExecutorTest(ServeurExecTest):
    FENCE = ("inge-front", OWNER, 42)

    def test_peek_publication_is_scoped_fenced_sanitised_and_not_readable_to_executor(self):
        name = "operations.publish_peek"
        payload = {"events": [{"kind": "message", "summary": "Ready TOKEN=PRIVATE"},
                              {"kind": "thought", "summary": "PRIVATE"}]}
        result = self.op(name, ["inge-front", OWNER, 42, payload], fence=self.FENCE)
        self.assertEqual(result.status, 200, result.body)
        self.assertTrue(result.body["value"])
        self.assertNotIn("PRIVATE", json.dumps(self.db.query("SELECT payload FROM agent_peek")))
        self.assertEqual(self.op("operations.read_peek", ["inge-front", "human:owner"]).status, 404)
        self.assertEqual(self.op(name, ["coord", OWNER, 42, payload],
                                 fence=("coord", OWNER, 42)).status, 403)
        self.hold("inge-front", OWNER, 43)
        stale = self.op(name, ["inge-front", OWNER, 42, payload], fence=self.FENCE)
        self.assertEqual(stale.status, 200)
        self.assertFalse(stale.body["value"])
        self.assertEqual(self.db.query("SELECT lease_epoch FROM agent_peek")[0]["lease_epoch"], 42)
