# SPDX-License-Identifier: AGPL-3.0-only
"""A paused train wakes the target project's pilot with actionable evidence."""
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.support import PgTestCase
from ameesh import train_runner


class PauseMailTest(unittest.TestCase):
    def test_red_pause_wakes_project_pilot_and_retries_idempotently(self):
        passage = object.__new__(train_runner.Passage)
        passage.cfg = object()
        passage.project = SimpleNamespace(name="nexlink", target="develop", github="manaty/nexlink")
        plan_id = "a" * 64
        state = {"phase": "paused", "plan": {"id": plan_id, "members": [
            {"work_item_id": 8087}, {"work_item_id": 8090}]},
            "attempts": [{"ci": {"workflows": [{"run": 45036027501}]}}]}
        runtime = mock.Mock()
        runtime.load.return_value = state
        db = object()
        with mock.patch("ameesh.train_canon.pilot_of", return_value=lambda db: "pilot"), \
             mock.patch("ameesh.mail.send") as send:
            passage._notify_paused(db, runtime, {"result": "paused"})
            passage._notify_paused(db, runtime, {"result": "paused"})
        args, kwargs = send.call_args
        self.assertEqual(args[:3], (db, "ameesh", "pilot"))
        self.assertNotIn("kind", kwargs)
        self.assertEqual(send.call_args_list[0].kwargs["idempotency_key"], kwargs["idempotency_key"])
        for text in (plan_id, "#8087", "#8090", "https://github.com/manaty/nexlink/actions/runs/45036027501", "train-faults:"):
            self.assertIn(text, args[3])

    def test_scheduled_pass_sends_when_engine_pauses(self):
        passage = object.__new__(train_runner.Passage)
        passage.cfg = object()
        passage.project = SimpleNamespace(name="nexlink", target="develop", github="manaty/nexlink")
        passage.policy = SimpleNamespace(enabled=True)
        runtime = mock.Mock()
        runtime.policy.schedule.next_packet.return_value = 10000
        runtime.load.return_value = {"phase": "paused", "plan": {
            "id": "b" * 64, "members": [{"work_item_id": 8087}]},
            "attempts": [{"ci": {"workflows": [{"run": 123}]}}]}
        passage.runtime = mock.Mock(return_value=runtime)
        passage.canon_refusal = mock.Mock(return_value=None)
        passage._step = mock.Mock(return_value={"result": "paused"})
        with mock.patch("ameesh.train_canon.pilot_of", return_value=lambda db: "pilot"), \
             mock.patch("ameesh.mail.send") as send:
            result, deadline = passage.scheduled_step(object(), 1)
        self.assertEqual(result["result"], "paused")
        self.assertLessEqual(deadline, 1 + train_runner.PAUSED_RECHECK_S)
        self.assertEqual(send.call_args.args[2], "pilot")
        self.assertIn("actions/runs/123", send.call_args.args[3])

    def test_other_results_do_not_send(self):
        passage = object.__new__(train_runner.Passage)
        runtime = mock.Mock()
        passage._notify_paused(None, runtime, {"result": "landed"})
        runtime.load.assert_not_called()


class PauseMailDatabaseTest(PgTestCase):
    def test_real_mail_deposit_and_idempotent_retry(self):
        passage = object.__new__(train_runner.Passage)
        passage.cfg = object()
        passage.project = SimpleNamespace(name="nexlink", target="develop", github="manaty/nexlink")
        runtime = mock.Mock()
        runtime.load.return_value = {"phase": "paused", "plan": {
            "id": "c" * 64, "members": [{"work_item_id": 8087}]},
            "attempts": []}
        with mock.patch("ameesh.train_canon.pilot_of", return_value=lambda db: "pilot"):
            passage._notify_paused(self.db, runtime, {"result": "paused"})
            passage._notify_paused(self.db, runtime, {"result": "paused"})
        rows = self.db.query("SELECT recipient, kind, body FROM agent_mailbox")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["recipient"], "pilot")
        self.assertEqual(rows[0]["kind"], "notify")
        self.assertIn("#8087", rows[0]["body"])
