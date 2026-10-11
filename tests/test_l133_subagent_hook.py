# SPDX-License-Identifier: AGPL-3.0-only
"""L133 : le courrier d'un agent n'est jamais remis à l'un de ses sous-agents.

Claude Code (outil Agent/Task, coéquipier en processus) et Codex
(`spawn_agent`) déclenchent les hooks ordinaires dans les sous-agents avec la
session et l'environnement de l'agent principal, et ajoutent `agent_id` à
l'entrée. Le hook ne lit, ne remet, ne marque ni n'écrit alors rien : la
session principale reçoit son courrier à son propre hook suivant."""
from __future__ import annotations

import io
import json
import sys
import unittest
from unittest import mock

from ameesh import cli, mail, registry

from .support import PgTestCase

#: identifiant de sous-agent tel que Claude Code le pose (relevé dans une
#: transcription de sous-agent du 2026-10-10)
SUBAGENT_ID = "a06213b3114ba1113"


def _payload(event: str, cwd: str, **extra: str) -> str:
    data = {"hook_event_name": event, "session_id": "s-principale", "cwd": cwd}
    data.update(extra)
    return json.dumps(data)


class SubagentFieldTest(unittest.TestCase):
    """Le champ qui désigne un sous-agent, et la sortie immédiate, sans base."""

    def test_agent_id_designates_a_subagent(self):
        self.assertEqual(cli.hook_subagent({"agent_id": SUBAGENT_ID,
                                            "agent_type": "Explore"}), SUBAGENT_ID)

    def test_main_session(self):
        for data in ({}, {"agent_id": None}, {"agent_id": ""}, {"agent_id": "  "},
                     # session principale lancée avec `claude --agent` :
                     # agent_type seul, sans agent_id
                     {"agent_type": "security-reviewer"}):
            with self.subTest(data=data):
                self.assertEqual(cli.hook_subagent(data), "")

    def _run_hook(self, payload: str) -> str:
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(cli.backend_mod, "open_backend",
                                  side_effect=AssertionError("base ouverte")), \
                mock.patch.object(cli.backend_mod, "FileBackend",
                                  side_effect=AssertionError("boîte fichier ouverte")):
            self.assertEqual(cli.cmd_hook(mock.sentinel.cfg, "claude"), 0)
        return out.getvalue()

    def test_subagent_hook_opens_nothing(self):
        for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
            with self.subTest(event=event):
                payload = _payload(event, "/ailleurs", agent_id=SUBAGENT_ID,
                                   agent_type="general-purpose")
                self.assertEqual(self._run_hook(payload), "")

    def test_input_that_is_not_an_object(self):
        self.assertEqual(self._run_hook("[1, 2]"), "")


class SubagentHookMailTest(PgTestCase):
    """Bout en bout : le sous-agent ne reçoit rien, la session principale tout."""

    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "alpha", harness="claude", host=self.cfg.host,
                        cwd=self.tmp, session_id="s-principale", mode="externe")
        self.db.execute("UPDATE agent_registry SET last_seen = now() - interval '1 hour'")
        mail.send(self.db, "beta", "alpha", "Pour la session principale.")

    def hook(self, harness: str, payload: str):
        return self.cli("hook", harness, env=self.env(AGENT_MAIL_NAME="alpha"),
                        stdin=payload)

    def undelivered(self) -> int:
        return self.db.query(
            "SELECT count(*)::int AS n FROM agent_mailbox "
            "WHERE recipient = 'alpha' AND delivered_at IS NULL")[0]["n"]

    def test_subagent_receives_and_marks_nothing(self):
        before = registry.get(self.db, "alpha")
        for harness in ("claude", "codex"):
            for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
                with self.subTest(harness=harness, event=event):
                    proc = self.hook(harness, _payload(
                        event, "/ailleurs/worktree-du-sous-agent",
                        agent_id=SUBAGENT_ID, agent_type="Explore"))
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout, "")
                    self.assertEqual(self.undelivered(), 1)
        # rien n'est écrit : ni dossier, ni session, ni signe de vie
        after = registry.get(self.db, "alpha")
        for field in ("cwd", "session_id", "harness", "host", "last_seen_ts"):
            self.assertEqual(after[field], before[field], field)

        # la session principale reçoit ensuite le message, et lui seul le marque
        proc = self.hook("claude", _payload("PostToolUse", self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Pour la session principale.", context)
        self.assertEqual(self.undelivered(), 0)

    def test_main_session_launched_with_agent_still_receives(self):
        # `claude --agent revue` : agent_type sans agent_id, c'est la session
        # principale — comportement inchangé
        proc = self.hook("claude", _payload("PostToolUse", self.tmp, agent_type="revue"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Pour la session principale.", context)
        self.assertEqual(self.undelivered(), 0)

    def test_main_session_stop_still_blocks(self):
        proc = self.hook("codex", _payload("Stop", self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        output = json.loads(proc.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertIn("Pour la session principale.", output["reason"])
        self.assertEqual(self.undelivered(), 0)


if __name__ == "__main__":
    unittest.main()
