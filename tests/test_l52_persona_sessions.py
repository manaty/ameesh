# SPDX-License-Identifier: AGPL-3.0-only
"""L52 (0032 §2): current and past persona sessions."""
from __future__ import annotations

import io
import json
import sys
import types
import unittest
from unittest import mock

from ameesh import cli, persona_sessions as ps, registry

from .support import PgTestCase


class SessionHistoryTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE persona_sessions RESTART IDENTITY")
        registry.upsert(self.db, "checker-a", harness="deepseek", host="pc", mode="execute")

    def test_new_session_replaces_previous(self):
        registry.set_session(self.db, "checker-a", "s1", "primary")
        registry.set_session(self.db, "checker-a", "s2", None)
        rows = ps.sessions(self.db, "checker-a")
        self.assertEqual([r["session_id"] for r in rows], ["s2", "s1"])
        self.assertIsNone(rows[0]["ended_at"])
        self.assertEqual(rows[1]["end_reason"], "replaced by s2")
        self.assertEqual((rows[1]["harness"], rows[1]["host"], rows[1]["account"]),
                         ("deepseek", "pc", "primary"))

    def test_resume_same_session(self):
        registry.set_session(self.db, "checker-a", "s1", "primary")
        ps.record_end(self.db, "checker-a", "s1", "stopped")
        registry.set_session(self.db, "checker-a", "s1", None)
        rows = ps.sessions(self.db, "checker-a")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["ended_at"])
        self.assertEqual(rows[0]["account"], "primary")     # original account retained

    def test_end_session(self):
        registry.set_session(self.db, "checker-a", "s1")
        ps.record_end(self.db, "checker-a", "s1", "rotation")
        ps.record_end(self.db, "checker-a", "s1", "other")       # already closed: unchanged
        self.assertEqual(ps.sessions(self.db, "checker-a")[0]["end_reason"], "rotation")
        ps.record_end(self.db, "checker-a", None, "none")          # no session: no change

    def test_rotation_closes_session(self):
        registry.set_session(self.db, "checker-a", "s1")
        self.db.execute("UPDATE agent_registry SET lease_owner = 'r', lease_epoch = 7, "
                        "lease_expires_at = now() + interval '5 minutes' WHERE name = 'checker-a'")
        self.assertTrue(registry.clear_session(self.db, "checker-a", "r", 7))
        self.assertEqual(ps.sessions(self.db, "checker-a")[0]["end_reason"], "rotation")

    def test_refused_rotation_keeps_session_open(self):
        registry.set_session(self.db, "checker-a", "s1")
        self.db.execute("UPDATE agent_registry SET lease_owner = 'r', lease_epoch = 7, "
                        "lease_expires_at = now() + interval '5 minutes' WHERE name = 'checker-a'")
        self.assertFalse(registry.clear_session(self.db, "checker-a", "other", 7))
        self.assertIsNone(ps.sessions(self.db, "checker-a")[0]["ended_at"])
        self.assertEqual(registry.get(self.db, "checker-a")["session_id"], "s1")

    def test_unaccepted_or_unknown_session_is_not_recorded(self):
        registry.set_session(self.db, "checker-a", "s1")
        ps.record_start(self.db, "checker-a", "child")
        ps.record_start(self.db, "unknown", "s1")
        self.assertEqual([r["session_id"] for r in ps.sessions(self.db, "checker-a")], ["s1"])
        self.assertIsNone(ps.sessions(self.db, "checker-a")[0]["ended_at"])
        self.assertEqual(ps.sessions(self.db, "unknown"), [])

    def test_failed_history_insert_does_not_close_previous_session(self):
        registry.set_session(self.db, "checker-a", "s1")
        # The registry accepts arbitrary IDs; the history constraint refuses
        # an oversized one. Replacement must roll back with that insertion.
        with mock.patch.object(sys, "stderr", io.StringIO()) as err:
            registry.set_session(self.db, "checker-a", "x" * 201)
        self.assertIn("session history could not be recorded", err.getvalue())
        rows = ps.sessions(self.db, "checker-a")
        self.assertEqual([r["session_id"] for r in rows], ["s1"])
        self.assertIsNone(rows[0]["ended_at"])

    def test_external_hook_records_only_accepted_session(self):
        registry.upsert(self.db, "external", harness="deepseek", host="pc", mode="externe")
        for session_id in ("main", "ignored"):
            proc = self.cli("hook", "deepseek", env=self.env(AGENT_MAIL_NAME="external"),
                            stdin=json.dumps({"hook_event_name": "SessionStart",
                                              "session_id": session_id, "cwd": self.tmp}))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("session history could not be recorded", proc.stderr)
        self.assertEqual(registry.get(self.db, "external")["session_id"], "main")
        self.assertEqual([r["session_id"] for r in ps.sessions(self.db, "external")], ["main"])


class HookHistoryFilterTest(unittest.TestCase):
    def run_hook(self, *, leased=False, agent_id=None):
        backend = mock.Mock(kind="pg")
        binding = types.SimpleNamespace(ok=True, name="checker", bound_to_lease=leased,
                                        runner_id="r", epoch=1, source="session")
        payload = {"hook_event_name": "Other", "session_id": "child-or-main", "cwd": "/w"}
        if agent_id is not None:
            payload["agent_id"] = agent_id
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(cli.backend_mod, "open_backend", return_value=(backend, None)), \
                mock.patch.object(cli.identity, "resolve_binding", return_value=binding), \
                mock.patch.object(cli, "_register_due", return_value=True), \
                mock.patch.object(cli, "_register_done"), \
                mock.patch.object(cli, "set_title"), \
                mock.patch.object(registry, "record_session_history") as record:
            self.assertEqual(cli.cmd_hook(mock.sentinel.cfg, "deepseek"), 0)
            return backend, record

    def test_external_main_hook_records_after_registration(self):
        backend, record = self.run_hook()
        backend.register.assert_called_once_with("checker", "deepseek", "/w", "child-or-main",
                                                leased=False)
        record.assert_called_once_with(backend.db, "checker", "child-or-main")

    def test_leased_child_hook_cannot_record_session_id(self):
        backend, record = self.run_hook(leased=True)
        backend.register.assert_called_once_with("checker", "deepseek", "/w", None, leased=True)
        record.assert_not_called()

    def test_explicit_subagent_hook_writes_nothing(self):
        backend, record = self.run_hook(agent_id="child")
        backend.register.assert_not_called()
        record.assert_not_called()


class NonBlockingHistoryTest(unittest.TestCase):
    def test_recording_failure_does_not_block_session_update(self):
        with mock.patch.object(registry.storage, "of") as store, \
                mock.patch.object(ps, "record_start", side_effect=RuntimeError("history unavailable")), \
                mock.patch.object(sys, "stderr", io.StringIO()) as err:
            registry.set_session(mock.sentinel.db, "checker", "s1")
        store.return_value.agents.set_session.assert_called_once_with("checker", "s1", None)
        self.assertIn("session history could not be recorded", err.getvalue())

    def test_history_snapshot_failure_does_not_block_rotation(self):
        db = mock.Mock()
        db.query.side_effect = RuntimeError("snapshot unavailable")
        with mock.patch.object(registry.storage, "of") as store, \
                mock.patch.object(sys, "stderr", io.StringIO()):
            store.return_value.leases.clear_session.return_value = True
            self.assertTrue(registry.clear_session(db, "checker", "r", 1))
        store.return_value.leases.clear_session.assert_called_once_with("checker", "r", 1)
