# SPDX-License-Identifier: AGPL-3.0-only
"""L78: replay, commit-ordered cursors, R12 retry, retention and bounded writer."""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
import unittest
from unittest import mock

from ameesh import conversation as C, db as db_mod, fil, progress, storage
from tests.support import PgTestCase


class UnitTest(unittest.TestCase):
    def test_tools_never_include_arguments_output_or_reasoning(self):
        secret = "DO NOT STORE THIS"
        raw = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "read", "input": {"password": secret}},
            {"type": "thinking", "thinking": secret}]}}
        self.assertEqual(C.tool_events(raw), [("read", "started")])
        raw = {"method": "item/completed", "params": {"item": {
            "type": "commandExecution", "aggregatedOutput": secret, "command": secret}}}
        self.assertEqual(C.tool_events(raw), [("commandExecution", "completed")])
        self.assertNotIn(secret, json.dumps(C.metadata({"output": secret, "tool": "read",
                                                       "cost_usd": float("nan")})))

    def test_reader_never_waits_for_writer_or_saturated_queue(self):
        gate = threading.Event()
        entered = threading.Event()
        db = mock.Mock()
        log = mock.Mock()
        def connect():
            entered.set()
            gate.wait(5)
            return db
        with mock.patch.object(C, "Log", return_value=log):
            recorder = C.Recorder(mock.Mock(), agent="worker", turn="t1", connect=connect,
                                  capacity=2, warn=mock.Mock())
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            for _ in range(100):
                recorder.on_event({"type": "text"}, {"display": ["delta"]})
            self.assertLess(time.monotonic() - start, 0.2)
            self.assertEqual(recorder.dropped, 98)
            gate.set()
            self.assertTrue(recorder.finish(final="answer"))
            self.assertTrue(recorder.close())
        texts = [call.args[1] for call in log.append.call_args_list if call.args[0] == "text"]
        self.assertEqual(texts, ["deltadelta"])
        self.assertTrue(any(call.kwargs.get("meta", {}).get("dropped") == 98
                            for call in log.append.call_args_list))
        db.close.assert_called_once()

    def test_idle_delta_flushes_at_half_second_and_principal_only(self):
        text_written = threading.Event()
        log = mock.Mock()
        def append(kind, *args, **kwargs):
            if kind == "text":
                text_written.set()
        log.append.side_effect = append
        with mock.patch.object(C, "Log", return_value=log):
            recorder = C.Recorder(mock.Mock(), agent="worker", turn="t2", connect=mock.Mock(),
                                  warn=mock.Mock())
            recorder.on_event({"parent_tool_use_id": "child"}, {"display": ["hidden"]})
            recorder.on_event({}, {"display": ["a", "b"]})
            self.assertFalse(text_written.wait(0.1))
            self.assertTrue(text_written.wait(1))
            recorder.close()
        texts = [call.args[1] for call in log.append.call_args_list if call.args[0] == "text"]
        self.assertEqual(texts, ["ab"])

    def test_history_checks_limits_before_storage(self):
        for args in ((-1, 10), (0, 0), (0, 501), (True, 10)):
            with self.assertRaises(ValueError):
                C.history(None, "worker", after=args[0], limit=args[1])


class StoreTest(PgTestCase):
    def setUp(self):
        self.db.execute("DELETE FROM conversation_log; DELETE FROM conversation_heads")
        self.cfg = dataclasses.replace(self.cfg, threads_dir=os.path.join(self.tmp, self._testMethodName))
        self.log = C.Log(self.db, self.cfg, agent="worker", project="acme", lot="78")

    def test_pages_replay_and_agent_scope(self):
        a = self.log.append("human_message", "question", author="human:alice", event_id="input1")
        again = self.log.append("human_message", "changed", author="human:bob", event_id="input1")
        self.assertEqual(again["seq"], a["seq"])
        self.assertEqual(again["text"], "question")
        self.log.append("text", "answer", session="s1", turn="t1")
        other = C.Log(self.db, self.cfg, agent="other", project="elsewhere")
        other.append("text", "private to other project")
        page = C.history(self.db, "worker", limit=1)
        self.assertEqual((page["last_seq"], page["next_seq"], page["more"], page["gap"]),
                         (2, 1, True, False))
        self.assertEqual([r["text"] for r in page["entries"]], ["question"])
        self.assertNotIn("thread_text", page["entries"][0])
        page = C.history(self.db, "worker", after=page["next_seq"])
        self.assertEqual([r["text"] for r in page["entries"]], ["answer"])
        self.assertFalse(page["more"])

    def test_notify_is_small_commit_only_and_replay_is_silent(self):
        listener = db_mod.listener(self.db, [C.CHANNEL])
        if listener is None:
            self.skipTest("driver has no LISTEN")
        self.addCleanup(listener.close)
        with self.db.transaction() as tx:
            log = C.Log(tx, self.cfg, agent="worker", project="acme")
            row = log.append("text", "sensitive " * 2000, event_id="1")
            self.assertIsNone(listener.wait(0.1))
        notification = listener.wait(3)
        self.assertIsNotNone(notification)
        self.assertEqual(json.loads(notification["payload"]), {"agent": "worker", "seq": row["seq"]})
        self.log.append("text", "ignored", event_id="1")
        self.assertIsNone(listener.wait(0.2))
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as tx:
                C.Log(tx, self.cfg, agent="worker", project="acme").append("text", "rolled back")
                raise RuntimeError("rollback")
        self.assertIsNone(listener.wait(0.2))

    def test_cursor_serializes_writers_through_commit(self):
        result, errors = [], []
        entered = threading.Event()
        def second():
            db = db_mod.connect(self.cfg)
            try:
                entered.set()
                result.append(C.Log(db, self.cfg, agent="worker", project="acme")
                              .append("text", "second"))
            except Exception as exc:
                errors.append(exc)
            finally:
                db.close()
        with self.db.transaction() as tx:
            first = C.Log(tx, self.cfg, agent="worker", project="acme").append("text", "first")
            thread = threading.Thread(target=second, daemon=True)
            thread.start()
            self.assertTrue(entered.wait(2))
            thread.join(timeout=0.2)
            self.assertTrue(thread.is_alive())
            self.assertEqual(result, [])
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0]["seq"], first["seq"] + 1)

    def test_thread_outbox_retry_and_retention_do_not_lose_human_or_final(self):
        self.log.append("human_message", "Hello", author="human:alice", event_id="q")
        self.log.append("turn_end", "Answer", thread_text="Answer", event_id="final:t1")
        self.log.append("tool", "read", meta={"output": "secret", "tool": "read"})
        self.db.execute("UPDATE conversation_log SET created_at = now() - interval '91 days'")
        with mock.patch.object(fil, "record", return_value=None):
            self.assertEqual(self.log.drain_threads(), 0)
        self.assertEqual(self.log.prune(), 1)
        self.assertEqual(len(self.log.store.pending_threads("worker")), 2)
        self.assertEqual(self.log.drain_threads(), 2)
        thread = fil.transport_for(self.cfg).read(fil.ThreadRef("acme", "78"))
        self.assertEqual([e.text for e in thread], ["Hello", "Answer"])
        self.assertEqual([e.author for e in thread], ["human:alice", "agent:worker"])
        self.assertEqual(self.log.prune(), 2)
        page = C.history(self.db, "worker", after=1)
        self.assertTrue(page["gap"])
        self.assertEqual(page["last_seq"], 3)
        agents = [{"name": "worker"}]
        C.attach_progress(self.db, agents)
        self.assertEqual(agents[0]["conversation"]["last_seq"], 3)
        self.assertEqual(agents[0]["conversation"]["entries"], [])
        self.assertEqual(self.log.append("state", "wake")["seq"], 4)

    def test_bounds_and_progress_export(self):
        self.log.append("text", "x" * (C.TEXT_LIMIT + 1))
        row = C.history(self.db, "worker")["entries"][0]
        self.assertEqual(len(row["text"]), C.TEXT_LIMIT)
        self.assertTrue(row["truncated"])
        agents = [{"name": "worker"}, {"name": "empty"}]
        C.attach_progress(self.db, agents, limit=1)
        self.assertEqual(agents[0]["conversation"]["last_seq"], 1)
        self.assertEqual(agents[1]["conversation"]["entries"], [])

    def test_real_recorder_uses_dedicated_connection_and_writes_final_to_thread(self):
        recorder = C.Recorder(self.cfg, agent="worker", turn="turn-1", project="acme", lot="78")
        recorder.on_event({"type": "session"}, {"session": "session-1"})
        recorder.on_event({"type": "text"}, {"display": ["Hello ", "world"]})
        recorder.on_event({"type": "tool", "name": "read", "status": "completed",
                           "output": "secret"}, {"display": ["[tool] read"]})
        recorder.finish(final="Done", cost=0.1, usage={"input_tokens": 10}, status="completed")
        self.assertTrue(recorder.close())
        self.assertIsNone(recorder.error)
        rows = C.history(self.db, "worker")["entries"]
        self.assertEqual([r["kind"] for r in rows], ["text", "tool", "turn_end"])
        self.assertEqual(rows[0]["text"], "Hello world")
        self.assertEqual(rows[-1]["meta"]["cost_usd"], 0.1)
        self.assertTrue(all(r["session"] == "session-1" for r in rows))
        self.assertNotIn("secret", json.dumps(rows))
        self.assertEqual([e.text for e in fil.transport_for(self.cfg).read(
            fil.ThreadRef("acme", "78"))], ["Done"])

    def test_one_turn_runner_and_progress_use_same_conversation(self):
        proc = self.register("worker", "claude", cwd=self.tmp, chantier="acme", prompt="work")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = self.runner("--once", "--agents", "worker")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = C.history(self.db, "worker")["entries"]
        self.assertEqual(rows[0]["kind"], "input")
        self.assertEqual(rows[-1]["kind"], "turn_end")
        self.assertEqual(rows[-1]["meta"]["status"], "completed")
        self.assertTrue(rows[-1]["text"])
        snap = progress.snapshot(self.db, self.cfg, project="acme", gantt=False)
        card = next(a for a in snap["agents"] if a["name"] == "worker")
        self.assertEqual(card["conversation"]["last_seq"], rows[-1]["seq"])
        self.assertEqual(card["conversation"]["entries"][-1]["text"], rows[-1]["text"])
