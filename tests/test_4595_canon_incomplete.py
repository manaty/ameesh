# SPDX-License-Identifier: AGPL-3.0-only
"""An incomplete shared sync explains its refusal without opening the gate."""
import types
import copy
import unittest
from unittest import mock

from ameesh import canon_sync
from .support import PgTestCase


class IncompleteDiagnosticTest(unittest.TestCase):
    def test_pending_and_different_snapshots_report_each_pass_then_recover(self):
        canon = types.SimpleNamespace(
            id="team", root="/agent/canon", is_default=True,
            untrusted=False, readable=True, load_findings=[],
            sources=[types.SimpleNamespace(member="root", repo="/agent/canon",
                                           commit="abc", mode="git")])
        store = mock.Mock()
        store.claim_sync.return_value = False
        store.shared_sync.side_effect = [None, {"sync_signature": "other"}]
        messages = []
        with mock.patch.object(canon_sync, "state_key", return_value=""), \
                mock.patch.object(canon_sync, "configured_ref", return_value="main"), \
                mock.patch.object(canon_sync.storage, "of",
                                  return_value=types.SimpleNamespace(canon=store)), \
                mock.patch.object(canon_sync, "sync") as sync:
            for _ in range(2):
                self.assertIsNone(canon_sync.periodic_sync(
                    None, canon, "host", 300, on_incomplete=messages.append))
            sync.assert_not_called()
            self.assertEqual(len(messages), 2)
            self.assertIn("pending or failed", messages[0])
            self.assertIn("snapshot mismatch", messages[1])
            for message in messages:
                self.assertIn("team at /agent/canon", message)
                self.assertIn("root=/agent/canon@abc", message)
                self.assertIn("interval 300.0s", message)
                self.assertIn('"trusted_ref": "main"', message)
                self.assertIn('"findings": []', message)
            store.claim_sync.return_value = True
            report = canon_sync.periodic_sync(
                None, canon, "host", 300, on_incomplete=messages.append)
            self.assertIs(report, sync.return_value)
            self.assertEqual(len(messages), 2)
            store.complete_sync.assert_called_once()


class RollingRestartTest(PgTestCase):
    """Independent runners retain the host's claim through a rolling restart.

    The two deployed versions use the same signature algorithm. A process
    version alone cannot invalidate a completed snapshot; changed inputs and
    an interrupted old claimant are separate cases, both kept fail closed.
    """

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM canon_state")
        self.old = types.SimpleNamespace(
            id="team", root="/agent/canon", is_default=True,
            untrusted=False, readable=True, load_findings=[],
            sources=[types.SimpleNamespace(member="root", repo="/agent/canon",
                                           commit="abc", mode="git")])
        self.new = copy.deepcopy(self.old)
        self.messages = []

    def run_sync(self, canon):
        return canon_sync.periodic_sync(
            self.db, canon, "rolling4595", 300,
            on_incomplete=self.messages.append)

    def expire_claim(self):
        self.db.execute("UPDATE canon_state SET sync_claimed_at = "
                        "clock_timestamp() - interval '301 seconds' "
                        "WHERE host = 'rolling4595'")

    def test_completed_old_runner_is_reused_until_inputs_change(self):
        with mock.patch.object(canon_sync, "state_key", return_value=""), \
                mock.patch.object(canon_sync, "configured_ref", return_value="main"), \
                mock.patch.object(canon_sync, "sync") as sync:
            self.run_sync(self.old)
            self.assertTrue(self.run_sync(self.new).shared)
            self.new.load_findings = ["new loader finding"]
            for _ in range(2):
                self.assertIsNone(self.run_sync(self.new))
            self.assertEqual(sync.call_count, 1)
            self.assertEqual(len(self.messages), 2)
            self.assertIn("snapshot mismatch", self.messages[0])
            self.assertIn('"findings": ["new loader finding"]', self.messages[0])
            self.expire_claim()
            self.assertIs(self.run_sync(self.new), sync.return_value)
            self.assertTrue(self.run_sync(self.new).shared)
            self.assertEqual(sync.call_count, 2)

    def test_stopped_old_claimant_recovers_after_claim_expiry(self):
        store = canon_sync.storage.of(self.db).canon
        self.assertTrue(store.claim_sync("rolling4595", "", "stopped-old-runner", 300))
        with mock.patch.object(canon_sync, "state_key", return_value=""), \
                mock.patch.object(canon_sync, "configured_ref", return_value="main"), \
                mock.patch.object(canon_sync, "sync") as sync:
            self.assertIsNone(self.run_sync(self.new))
            self.assertIn("no completed token", self.messages[0])
            sync.assert_not_called()
            self.expire_claim()
            self.assertIs(self.run_sync(self.new), sync.return_value)
            # A late completion by the old process cannot replace the new one.
            store.complete_sync("rolling4595", "", "stopped-old-runner", "old")
            self.assertTrue(self.run_sync(self.new).shared)
            self.assertEqual(sync.call_count, 1)
