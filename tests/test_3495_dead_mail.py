# SPDX-License-Identifier: AGPL-3.0-only
"""#3495 : une observation de runner ne devient pas une boîte destinataire."""
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import mail, plan_git, planned_waits, registry


class RetentionRecipientTest(unittest.TestCase):
    def setUp(self):
        self.runner = "runner runner-mty-amh-impl-08@ameesh-agents-2"
        self.row = {"id": 3444, "state": "blocked", "assignee": "dev-a"}
        self.events = [
            {"id": 1, "work_item_id": 3444, "state": "blocked",
             "actor": "mesh-design", "note": "hold: frontière attendue", "created_ts": 1},
            {"id": 2, "work_item_id": 3444, "state": "blocked",
             "actor": self.runner, "note": "merge seen by branch", "created_ts": 2},
        ]
        self.st = SimpleNamespace(db=None, progress=SimpleNamespace(
            lot_hold_events=lambda *args: self.events))

    def wake(self, retention):
        def get(db, name):
            return {"name": name, "status": "idle"} if name in ("mesh-design", "dev-a") else None
        with mock.patch.object(registry, "get", side_effect=get), \
                mock.patch.object(mail, "send") as send:
            plan_git._wake_retention(None, self.row, retention, entry={}, target="main")
        return send

    def test_blocked_observation_notifies_active_hold_author(self):
        with mock.patch.object(planned_waits, "_pilot_allowed", return_value=lambda who: True):
            retention = plan_git.content_retention(self.st, self.row)
        self.assertEqual(retention["what"], "blocked")
        self.assertEqual(retention["by"], "mesh-design")
        self.assertEqual(self.wake(retention).call_args.args[2], "mesh-design")

    def test_without_hold_blocked_transition_notifies_assignee(self):
        self.events = self.events[1:]
        retention = plan_git.content_retention(self.st, self.row)
        self.assertEqual(retention["by"], "dev-a")
        self.assertEqual(self.wake(retention).call_args.args[2], "dev-a")

    def test_legacy_runner_or_unknown_recipient_falls_back_to_assignee(self):
        for by in (self.runner, "missing-agent", "ameesh"):
            with self.subTest(by=by):
                self.assertEqual(self.wake({"by": by, "what": "hold", "motif": "test"})
                                 .call_args.args[2], "dev-a")

    def test_human_is_not_notified_or_delegated(self):
        self.wake({"by": "human:owner", "what": "hold", "motif": "test"}).assert_not_called()

    def test_unknown_assignee_does_not_create_dead_mail(self):
        self.row["assignee"] = "unknown"
        self.wake({"by": self.runner, "what": "hold", "motif": "test"}).assert_not_called()
