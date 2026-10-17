# SPDX-License-Identifier: AGPL-3.0-only
"""L52 (0032 §2): current and past persona sessions."""
from __future__ import annotations

import io
import json
import sys
import unittest
from unittest import mock

from ameesh import cli, db as db_mod, persona_sessions as ps, registry, storage

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

    def test_injected_history_query_canceled_preserves_registry_transitions(self):
        registry.set_session(self.db, "checker-a", "s1")
        self.db.execute(
            "CREATE FUNCTION cancel_history_write() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'injected history cancellation' "
            "USING ERRCODE = '57014'; END $$")
        self.db.execute(
            "CREATE TRIGGER cancel_history_write BEFORE INSERT OR UPDATE ON persona_sessions "
            "FOR EACH ROW EXECUTE FUNCTION cancel_history_write()")
        try:
            with mock.patch.object(sys, "stderr", io.StringIO()) as err:
                registry.set_session(self.db, "checker-a", "s2")
            self.assertIn("injected history cancellation", err.getvalue())
            self.assertEqual(registry.get(self.db, "checker-a")["session_id"], "s2")
            self.assertEqual([r["session_id"] for r in ps.sessions(self.db, "checker-a")], ["s1"])
            self.assertIsNone(ps.sessions(self.db, "checker-a")[0]["ended_at"])
            # Close an existing history row as well as replacing it.
            with mock.patch.object(sys, "stderr", io.StringIO()):
                registry.set_session(self.db, "checker-a", "s1")
            with mock.patch.object(sys, "stderr", io.StringIO()) as err:
                storage.of(self.db).operations.resume(
                    "checker-a", prompt=None, forget=True, status_text="resume")
            self.assertIn("injected history cancellation", err.getvalue())
            self.assertIsNone(registry.get(self.db, "checker-a")["session_id"])
            self.assertIsNone(ps.sessions(self.db, "checker-a")[0]["ended_at"])
        finally:
            self.db.execute("DROP TRIGGER cancel_history_write ON persona_sessions")
            self.db.execute("DROP FUNCTION cancel_history_write()")

    def test_locked_history_statement_timeout_preserves_registry_transition(self):
        registry.set_session(self.db, "checker-a", "s1")
        blocker = db_mod.connect(self.cfg)
        try:
            with blocker.transaction() as locked:
                locked.query("SELECT id FROM persona_sessions WHERE persona = 'checker-a' FOR UPDATE")
                with self.db.transaction() as tx:
                    tx.execute("SET LOCAL statement_timeout = '100ms'")
                    with mock.patch.object(sys, "stderr", io.StringIO()) as err:
                        registry.set_session(tx, "checker-a", "s2")
                    self.assertIn("statement timeout", err.getvalue())
                    self.assertEqual(registry.get(tx, "checker-a")["session_id"], "s2")
            self.assertEqual(registry.get(self.db, "checker-a")["session_id"], "s2")
            self.assertEqual([r["session_id"] for r in ps.sessions(self.db, "checker-a")], ["s1"])
            self.assertIsNone(ps.sessions(self.db, "checker-a")[0]["ended_at"])
        finally:
            blocker.close()

    def test_session_id_is_data_in_best_effort_history_sql(self):
        session_id = "s' ; $history$ \\ quoted"
        registry.set_session(self.db, "checker-a", session_id)
        self.assertEqual(registry.get(self.db, "checker-a")["session_id"], session_id)
        self.assertEqual(ps.sessions(self.db, "checker-a")[0]["session_id"], session_id)

    def test_adopt_tracks_replacement_without_runner_turn(self):
        registry.set_session(self.db, "checker-a", "s1")
        result = storage.of(self.db).operations.adopt(
            "checker-a", harness="codex", host="other", cwd=None,
            session_id="s2", session_account="adopted", prompt=None, status_text="adopt")
        self.assertIsNotNone(result)
        rows = {r["session_id"]: r for r in ps.sessions(self.db, "checker-a")}
        self.assertEqual(rows["s1"]["end_reason"], "replaced by s2")
        self.assertIsNone(rows["s2"]["ended_at"])
        self.assertEqual((rows["s2"]["harness"], rows["s2"]["host"], rows["s2"]["account"]),
                         ("codex", "other", "adopted"))

    def test_restart_closes_history_without_runner_turn(self):
        registry.set_session(self.db, "checker-a", "s1")
        ops = storage.of(self.db).operations
        ops.request_restart("checker-a", "new brief")
        self.assertIsNotNone(ops.apply_restart("checker-a"))
        self.assertIsNone(registry.get(self.db, "checker-a")["session_id"])
        self.assertEqual(ps.sessions(self.db, "checker-a")[0]["end_reason"], "restart")

    def test_fresh_resume_closes_history_without_runner_turn(self):
        registry.set_session(self.db, "checker-a", "s1")
        self.assertIsNotNone(storage.of(self.db).operations.resume(
            "checker-a", prompt="fresh", forget=True, status_text="resume"))
        self.assertIsNone(registry.get(self.db, "checker-a")["session_id"])
        self.assertEqual(ps.sessions(self.db, "checker-a")[0]["end_reason"], "fresh resume")

    def test_refused_operations_preserve_history(self):
        registry.set_session(self.db, "checker-a", "s1")
        self.db.execute("UPDATE agent_registry SET lease_owner = 'r', lease_epoch = 7, "
                        "lease_expires_at = now() + interval '5 minutes', "
                        "status = 'running' WHERE name = 'checker-a'")
        ops = storage.of(self.db).operations
        self.assertIsNone(ops.adopt("checker-a", harness="codex", host="other", cwd=None,
                                   session_id="s2", session_account=None, prompt=None,
                                   status_text="adopt"))
        self.assertIsNone(ops.resume("checker-a", prompt=None, forget=True, status_text="resume"))
        ops.request_restart("checker-a", "new brief")
        self.assertIsNone(ops.apply_restart("checker-a", "other", 7))
        self.assertEqual([r["session_id"] for r in ps.sessions(self.db, "checker-a")], ["s1"])
        self.assertIsNone(ps.sessions(self.db, "checker-a")[0]["ended_at"])

    def test_history_close_holds_registry_lock_until_commit(self):
        registry.set_session(self.db, "checker-a", "s1")
        self.db.execute("UPDATE agent_registry SET lease_owner = 'r', lease_epoch = 7, "
                        "lease_expires_at = now() + interval '5 minutes' WHERE name = 'checker-a'")
        replacement = db_mod.connect(self.cfg)
        original = ps.record_end
        def delayed_end(*args, **kwargs):
            # A replacement trying to resume s1 at the old race boundary must
            # wait for the clear AND history close, on either SQL driver.
            with self.assertRaises(db_mod.DbError):
                with replacement.transaction() as tx:
                    tx.execute("SET LOCAL lock_timeout = '100ms'")
                    registry.set_session(tx, "checker-a", "s1")
            original(*args, **kwargs)
        try:
            with mock.patch.object(ps, "record_end", side_effect=delayed_end):
                self.assertTrue(registry.clear_session(self.db, "checker-a", "r", 7))
            registry.set_session(replacement, "checker-a", "s1")
            self.assertEqual(registry.get(self.db, "checker-a")["session_id"], "s1")
            self.assertIsNone(ps.sessions(self.db, "checker-a")[0]["ended_at"])
        finally:
            replacement.close()

    def test_missing_history_table_does_not_block_atomic_clear(self):
        registry.set_session(self.db, "checker-a", "s1")
        self.db.execute("UPDATE agent_registry SET lease_owner = 'r', lease_epoch = 7, "
                        "lease_expires_at = now() + interval '5 minutes' WHERE name = 'checker-a'")
        self.db.execute("ALTER TABLE persona_sessions RENAME TO history_unavailable")
        try:
            with mock.patch.object(sys, "stderr", io.StringIO()) as err:
                self.assertTrue(registry.clear_session(self.db, "checker-a", "r", 7))
            self.assertIn("session history could not be recorded", err.getvalue())
            self.assertIsNone(registry.get(self.db, "checker-a")["session_id"])
        finally:
            self.db.execute("ALTER TABLE history_unavailable RENAME TO persona_sessions")

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
        # #1719 : le double suit le contrat reel de `identity.Binding` — dont
        # `session`, lu par cmd_hook depuis #1604 ; `bound_to_lease` y est une
        # propriete, elle vient donc de `source` et non d'un champ pose a la main.
        binding = cli.identity.Binding(ok=True, name="checker", runner_id="r", epoch=1,
                                       source="runner" if leased else "session")
        payload = {"hook_event_name": "Other", "session_id": "child-or-main", "cwd": "/w"}
        if agent_id is not None:
            payload["agent_id"] = agent_id
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(cli.backend_mod, "open_backend", return_value=(backend, None)), \
                mock.patch.object(cli.identity, "resolve_binding", return_value=binding), \
                mock.patch.object(cli.fastmail, "remember_liaison") as remember, \
                mock.patch.object(cli, "_register_due", return_value=True), \
                mock.patch.object(cli, "_register_done"), \
                mock.patch.object(cli, "set_title"), \
                mock.patch.object(registry, "record_session_history") as record:
            self.assertEqual(cli.cmd_hook(mock.sentinel.cfg, "deepseek"), 0)
            return backend, record, remember

    def test_external_main_hook_records_after_registration(self):
        backend, record, remember = self.run_hook()
        backend.register.assert_called_once_with("checker", "deepseek", "/w", "child-or-main",
                                                leased=False)
        record.assert_called_once_with(backend.db, "checker", "child-or-main")
        remember.assert_called_once_with(mock.sentinel.cfg, "checker", "deepseek",
                                         "child-or-main", pid=None)

    def test_leased_child_hook_cannot_record_session_id(self):
        backend, record, remember = self.run_hook(leased=True)
        backend.register.assert_called_once_with("checker", "deepseek", "/w", None, leased=True)
        record.assert_not_called()
        remember.assert_not_called()

    def test_explicit_subagent_hook_writes_nothing(self):
        backend, record, remember = self.run_hook(agent_id="child")
        backend.register.assert_not_called()
        record.assert_not_called()
        remember.assert_not_called()


class NonBlockingHistoryTest(unittest.TestCase):
    def test_hook_history_failure_is_logged(self):
        with mock.patch.object(ps, "record_start", side_effect=RuntimeError("history unavailable")), \
                mock.patch.object(sys, "stderr", io.StringIO()) as err:
            registry.record_session_history(mock.sentinel.db, "checker", "s1")
        self.assertIn("session history could not be recorded", err.getvalue())

    def test_clear_does_not_read_history_outside_fenced_storage(self):
        db = mock.Mock()
        db.query.side_effect = RuntimeError("snapshot unavailable")
        with mock.patch.object(registry.storage, "of") as store, \
                mock.patch.object(sys, "stderr", io.StringIO()):
            store.return_value.leases.clear_session.return_value = True
            self.assertTrue(registry.clear_session(db, "checker", "r", 1))
        store.return_value.leases.clear_session.assert_called_once_with("checker", "r", 1)
        db.query.assert_not_called()
