# SPDX-License-Identifier: AGPL-3.0-only
"""#5493: a restarted runner settles on the last completed sync while a
neighbour's newer sync is pending; a failed claimed sync closes and retries."""
import types
from unittest import mock

from ameesh import canon_sync
from .support import PgTestCase

HOST = "pending5493"


class PendingReuseTest(PgTestCase):

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM canon_state WHERE host = %s", (HOST,))
        self.canon = types.SimpleNamespace(
            id="team", root="/agent/canon", is_default=True,
            untrusted=False, readable=True, load_findings=[],
            sources=[types.SimpleNamespace(member="root", repo="/agent/canon",
                                           commit="abc", mode="git")])
        self.store = canon_sync.storage.of(self.db).canon
        self.messages = []
        for target, value in (("state_key", ""), ("configured_ref", "main")):
            patcher = mock.patch.object(canon_sync, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_sync(self, canon=None):
        return canon_sync.periodic_sync(
            self.db, canon or self.canon, HOST, 300,
            on_incomplete=self.messages.append)

    def expire_claim(self):
        self.db.execute("UPDATE canon_state SET sync_claimed_at = "
                        "clock_timestamp() - interval '301 seconds' WHERE host = %s",
                        (HOST,))

    def test_pending_newer_claim_keeps_last_completed_result(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.run_sync()
            self.expire_claim()
            # a neighbour claims and is still syncing (minutes in staging)
            self.assertTrue(self.store.claim_sync(HOST, "", "neighbour", 300))
            report = self.run_sync()
            self.assertTrue(report.shared)
            self.assertEqual(sync.call_count, 1)
            self.assertEqual(self.messages, [])
            # a different snapshot is still refused while pending
            changed = types.SimpleNamespace(**vars(self.canon))
            changed.load_findings = ["new finding"]
            self.assertIsNone(self.run_sync(changed))
            self.assertIn("snapshot mismatch", self.messages[0])

    def test_failed_claimed_sync_closes_then_retries_without_waiting(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.run_sync()
            self.expire_claim()
            sync.side_effect = RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                self.run_sync()
            # the failure withdrew the completed result: no reuse
            self.assertIsNone(self.store.shared_sync(HOST, ""))
            # and released the claim: the next pass syncs at once
            sync.side_effect = None
            self.assertIs(self.run_sync(), sync.return_value)
            self.assertEqual(sync.call_count, 3)
            self.assertTrue(self.run_sync().shared)

    def test_stale_claimant_failure_does_not_touch_current_claim(self):
        with mock.patch.object(canon_sync, "sync"):
            self.run_sync()
        self.store.fail_sync(HOST, "", "someone-else")
        self.assertIsNotNone(self.store.shared_sync(HOST, ""))
        self.assertFalse(self.store.claim_sync(HOST, "", "other", 300))
