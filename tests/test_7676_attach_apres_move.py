# SPDX-License-Identifier: AGPL-3.0-only
"""#7676: local attach after a conversation move, and its lease on signals.

Three defects of the avatar qualification (#7635):
(a) between the switch and the target's first turn, `agent_registry.session_id`
    is still the source host's session; `ameesh attach` must never resume it
    here. It refuses with the move named, after taking (and returning) the
    lease, so a concurrent switch cannot slip between the check and the launch;
(b) a local attach must treat SIGTERM/SIGHUP like the distant one: stop the
    harness and return the lease instead of dying outside the `finally`;
(c) a declared Claude profile that never completed the interactive onboarding
    opens on the theme screen: the attach refuses with the one-time gesture
    instead of launching a session whose first message would be lost, and
    `ameesh doctor --harness` reports the profiles that are not ready.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import signal
import threading
import time
import unittest
import uuid
from unittest import mock

from ameesh import accounts, hostcheck, registry, storage
from ameesh import conversation_move as M
from ameesh.runner import run_attach

from .support import FAKEBIN, PgTestCase

READY = {check: True for check in M.CHECKS}


def ready(_move):
    return READY


class AttachAfterMoveTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM conversation_moves; DELETE FROM conversation_log;"
                        " DELETE FROM conversation_heads")
        self.register("att76", "claude", cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET session_id = 'sess-source',"
                        " host = 'source' WHERE name = 'att76'")
        self.lease = registry.claim(self.db, "att76", "runner-source", 300,
                                    host="source")
        self.assertEqual(self.turns(), [])

    def _move(self, target: str = "") -> dict:
        return M.plan(self.db, "att76", target or self.cfg.host,
                      requested_by="test")

    def _advance(self, target: str = "") -> None:
        steps = M.advance(self.db, target or self.cfg.host, probes=ready,
                          cwd_for=lambda _move: self.tmp)
        self.assertEqual([step for _ident, step in steps],
                         ["prepared", "switched"])

    def test_attach_refuses_a_switched_move_without_resuming_the_source(self):
        move = self._move()
        self._advance()
        row = registry.get(self.db, "att76")
        self.assertEqual((row["host"], row["session_id"]),
                         (self.cfg.host, "sess-source"))
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": self._fakebin(),
                                          "AMEESH_TEST_SLEEP": "1"}), \
                mock.patch("sys.stderr") as err:
            code = run_attach(self.cfg, self.db, "att76", ttl=30)
        self.assertEqual(code, 3)
        self.assertEqual(self.turns(), [], "the source session was launched")
        message = "".join(str(call.args[0]) for call in err.write.call_args_list)
        self.assertIn("conversation move", message)
        self.assertIn(move["move_id"], message)
        self.assertIn("first turn", message)
        self.assertIn("source", message)
        self.assertIsNone(registry.get(self.db, "att76")["lease_owner"])
        self.assertEqual(registry.get(self.db, "att76")["session_id"],
                         "sess-source")
        self.assertEqual(self.db.query(
            "SELECT state FROM conversation_moves WHERE move_id = %s",
            (move["move_id"],))[0]["state"], "switched")

    def test_attach_refuses_a_planned_move_so_it_can_switch(self):
        registry.release(self.db, "att76", "runner-source",
                         int(self.lease["lease_epoch"]))
        self.db.execute("UPDATE agent_registry SET host = %s WHERE name = 'att76'",
                        (self.cfg.host,))
        self.lease = registry.claim(self.db, "att76", "runner-source", 300,
                                    host=self.cfg.host)
        move = self._move("target")
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": self._fakebin()}), \
                mock.patch("sys.stderr") as err:
            code = run_attach(self.cfg, self.db, "att76", ttl=30)
        self.assertEqual(code, 3)
        self.assertEqual(self.turns(), [])
        message = "".join(str(call.args[0]) for call in err.write.call_args_list)
        self.assertIn(move["move_id"], message)
        self.assertIn("in progress", message)
        self.assertIsNone(registry.get(self.db, "att76")["lease_owner"])
        self.assertEqual(storage.of(self.db).conversation_moves.get(
            move["move_id"])["state"], "planned")

    def test_attach_resumes_the_verified_target_session(self):
        self._move()
        self._advance()
        lease = registry.claim(self.db, "att76", "runner-target", 300,
                               host=self.cfg.host)
        first = M.open_first_turn(self.db, self.cfg, self.cfg.agent_dir("att76"),
                                  agent="att76", host=self.cfg.host,
                                  owner="runner-target",
                                  epoch=lease["lease_epoch"])
        first.launched("sess-target")
        first.finish(True, session="sess-target", account=None,
                     harness="claude", cwd=self.tmp)
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": self._fakebin(),
                                          "AMEESH_TEST_LOG": self.turns_log,
                                          "AMEESH_TEST_SLEEP": "0.2"}):
            self.assertEqual(run_attach(self.cfg, self.db, "att76", ttl=30), 0)
        self.assertIn("sess-target", self.turns()[0]["argv"])

    def _fakebin(self) -> str:
        return FAKEBIN


class LocalAttachSignalTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register("sig76", "claude", cwd=self.tmp)

    def _attach_under_signal(self, signum: int) -> None:
        handlers = {}
        armed = threading.Event()

        def catch(number, handler):
            if callable(handler) and number in (signal.SIGTERM, signal.SIGHUP,
                                                signal.SIGINT):
                handlers[number] = handler
                if len(handlers) == 3:
                    armed.set()
            return signal.SIG_DFL

        def stopper():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not self.turns():
                row = registry.get(self.db, "sig76") or {}
                if (row.get("lease_owner") or "").startswith("attach:") \
                        and self.turns():
                    break
                time.sleep(0.05)
            self.assertTrue(self.turns(), "the harness was not launched")
            self.assertTrue(armed.wait(5), "the stop handlers were not armed")
            handlers[signum](signum, None)

        killer = threading.Thread(target=stopper, daemon=True)
        killer.start()
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                          "AMEESH_TEST_LOG": self.turns_log,
                                          "AMEESH_TEST_SLEEP": "20"}), \
                mock.patch("ameesh.runner.signal.signal", side_effect=catch):
            code = run_attach(self.cfg, self.db, "sig76", ttl=30)
        killer.join(5)
        self.assertEqual(code, 0)
        self.assertIsNone(registry.get(self.db, "sig76")["lease_owner"])

    def test_sigterm_stops_the_harness_and_returns_the_lease(self):
        self._attach_under_signal(signal.SIGTERM)

    def test_sighup_stops_the_harness_and_returns_the_lease(self):
        self._attach_under_signal(signal.SIGHUP)


def _interactive_account(path: str) -> str:
    """Un dossier de compte Claude propriétaire et privé, sans accueil."""
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)
    credentials = os.path.join(path, ".credentials.json")
    with open(credentials, "w", encoding="utf-8"):
        pass
    os.chmod(credentials, 0o600)
    return path


class InteractiveOnboardingTest(PgTestCase):
    """#7676(c): the first interactive launch must not land in the theme menu."""

    def setUp(self):
        super().setUp()
        self.register("onb76", "claude", cwd=self.tmp)
        self.account = _interactive_account(
            os.path.join(self.tmp, "comptes",
                         "claude-onb-%s" % uuid.uuid4().hex[:8]))
        self.cfg = dataclasses.replace(self.cfg, accounts={"claude": [
            {"name": "primaire", "path": self.account}]})
        self.db.execute(
            "UPDATE agent_registry SET session_id = %s, session_account = 'primaire' "
            "WHERE name = 'onb76'", (str(uuid.uuid4()),))
        patch = mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                             "AMEESH_TEST_LOG": self.turns_log})
        patch.start()
        self.addCleanup(patch.stop)

    def _complete_onboarding(self) -> None:
        path = os.path.join(self.account, ".claude.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({accounts.CLAUDE_INTERACTIVE_FLAG: True}, handle)
        os.chmod(path, 0o600)

    def test_attach_refuses_before_the_first_interactive_launch(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run_attach(self.cfg, self.db, "onb76", ttl=30)
        self.assertEqual(code, 1, err.getvalue())
        self.assertEqual(self.turns(), [], "the harness was launched into onboarding")
        message = err.getvalue()
        self.assertIn("not ready for an interactive session", message)
        self.assertIn(accounts.CLAUDE_INTERACTIVE_FLAG, message)
        self.assertIn("CLAUDE_CONFIG_DIR=%s claude" % self.account, message)
        self.assertIsNone(registry.get(self.db, "onb76")["lease_owner"])

    def test_attach_runs_once_the_profile_completed_onboarding(self):
        self._complete_onboarding()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run_attach(self.cfg, self.db, "onb76", ttl=30)
        self.assertEqual(code, 0, err.getvalue())
        turns = self.turns()
        self.assertEqual(len(turns), 1, err.getvalue())
        self.assertEqual(turns[0]["compte"]["CLAUDE_CONFIG_DIR"], self.account)

    def test_only_declared_claude_profiles_are_judged(self):
        synthetic = accounts.Profile(harness="claude", name="(default)")
        self.assertEqual(accounts.interactive_problem(synthetic), "")
        codex = accounts.Profile(harness="codex", name="secondaire",
                                 path=self.account, declared=True)
        self.assertEqual(accounts.interactive_problem(codex), "")
        declared = accounts.Profile(harness="claude", name="secondaire",
                                    path=self.account, declared=True)
        with open(os.path.join(self.account, ".claude.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({}, handle)
        self.assertIn(accounts.CLAUDE_INTERACTIVE_FLAG,
                      accounts.interactive_problem(declared))

    def test_default_profile_reads_the_home_claude_json(self):
        default = accounts.Profile(harness="claude", name="default", declared=True)
        with mock.patch.dict(os.environ, {"HOME": "/home/someone"}):
            self.assertEqual(accounts.interactive_config_file(default),
                             "/home/someone/.claude.json")


class InteractiveDoctorTest(PgTestCase):
    """#7676(c): `doctor --harness` reports the profiles still to initialize."""

    def _cfg(self, path: str):
        return dataclasses.replace(self.cfg, accounts={"claude": [
            {"name": "secondaire", "path": path}]})

    def test_lines_warn_until_ready_without_changing_the_verdict(self):
        path = _interactive_account(os.path.join(self.tmp, "comptes", "claude-doc"))
        lines = hostcheck.interactive_lines(self._cfg(path))
        text = "\n".join(lines)
        self.assertIn("warning", text)
        self.assertIn("claude/secondaire", text)
        self.assertIn(accounts.CLAUDE_INTERACTIVE_FLAG, text)
        config = os.path.join(path, ".claude.json")
        with open(config, "w", encoding="utf-8") as handle:
            json.dump({accounts.CLAUDE_INTERACTIVE_FLAG: True}, handle)
        lines = hostcheck.interactive_lines(self._cfg(path))
        self.assertIn("claude profile(s) ready", "\n".join(lines))

    def test_no_declared_profile_is_not_an_alarm(self):
        lines = hostcheck.interactive_lines(self.cfg)
        self.assertEqual(lines, ["interactive : no declared claude profile"])


if __name__ == "__main__":
    unittest.main()
