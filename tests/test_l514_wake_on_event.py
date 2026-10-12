# SPDX-License-Identifier: AGPL-3.0-only
"""L514: during a dsh or codex turn the runner drives, the PostToolUse hook
(the safe point between two tool calls) delivers only verdicts,
arbitrations, human and urgent mail; ordinary mail waits for the end of the
turn (L125). Claude keeps getting all of it. Real Postgres.
"""
from __future__ import annotations

import json
import os
import unittest

from ameesh import cli, mail, registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


class MidTurnTest(unittest.TestCase):
    def test_which_mail_does_not_wait(self):
        for payload, expected in (
                ({}, False), ({"ack": True}, False), ({"cc": "md"}, False),
                ({"verdict": True}, True), ({"arbitration": True}, True),
                ({"human": True, "decision_answer": {"decision": 1}}, True),
                ({"urgent": True}, True)):
            with self.subTest(payload=payload):
                self.assertIs(mail.mid_turn({"payload": payload}), expected)


class _Base(PgTestCase):
    def _leased(self, name: str, harness: str) -> AgentWorker:
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, harness, cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def _hook(self, worker: AgentWorker, harness: str, event: str):
        env = self.env(AGENT_MAIL_NAME=worker.name,
                       AMEESH_RUNNER_ID=worker.runner.runner_id,
                       AMEESH_LEASE_EPOCH=str(worker.epoch), AMEESH_HARNESS=harness)
        proc = self.cli("hook", harness, env=env, stdin=json.dumps(
            {"hook_event_name": event, "session_id": "s-1", "cwd": self.tmp}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _pending(self, name: str) -> list[str]:
        return [row["body"] for row in self.db.query(
            "SELECT body FROM agent_mailbox WHERE recipient = %s AND delivered_at IS NULL "
            "ORDER BY id", (name,))]


class MidTurnHookTest(_Base):
    def _mail(self, name: str) -> None:
        mail.send(self.db, "mty-amh-impl-02", name, "Ordinary progress note.")
        mail.send(self.db, "codex-review", name, "Verdict r2: two findings.",
                  payload={"verdict": True})
        mail.send(self.db, "mesh-design", name, "Arbitration: keep option b.",
                  payload={"arbitration": True})

    def test_dsh_turn_gets_the_verdict_between_two_tool_calls(self):
        for harness in ("deepseek", "codex"):
            with self.subTest(harness=harness):
                name = "ds-" + harness
                worker = self._leased(name, harness)
                self._mail(name)
                out = self._hook(worker, harness, "PostToolUse")
                context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
                self.assertIn("Verdict r2: two findings.", context)
                self.assertIn("Arbitration: keep option b.", context)
                self.assertNotIn("Ordinary progress note.", context)
                # released, not delivered: it waits for the end of the turn
                self.assertEqual(self._pending(name), ["Ordinary progress note."])
                self.assertEqual(self._hook(worker, harness, "PostToolUse"), "")
                stop = json.loads(self._hook(worker, harness, "Stop"))
                self.assertEqual(stop["decision"], "block")
                self.assertIn("Ordinary progress note.", stop["reason"])
                self.assertEqual(self._pending(name), [])

    def test_claude_turn_still_gets_everything(self):
        worker = self._leased("cl", "claude")
        self._mail("cl")
        context = json.loads(self._hook(worker, "claude", "PostToolUse")
                             )["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Ordinary progress note.", context)
        self.assertEqual(self._pending("cl"), [])


class SendMarksTest(_Base):
    def test_verdict_and_arbitration_flags(self):
        self.register("rx", "deepseek")
        for flag in ("--verdict", "--arbitration"):
            with self.subTest(flag=flag):
                proc = self.cli("send", "rx", "Result of the review.", flag,
                                env=self.env(AGENT_MAIL_NAME="codex-review"))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                row = self.db.query(
                    "SELECT payload FROM agent_mailbox WHERE recipient = 'rx' "
                    "ORDER BY id DESC LIMIT 1")[0]
                self.assertTrue(mail.mid_turn(row))
                self.assertNotIn("ack", mail.message_payload(row))

    def test_ack_contradicts_a_verdict(self):
        self.register("rx", "deepseek")
        proc = self.cli("send", "rx", "thanks", "--ack", "--verdict",
                        env=self.env(AGENT_MAIL_NAME="codex-review"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("contradict", proc.stderr)


if __name__ == "__main__":
    unittest.main()
