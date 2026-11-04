# SPDX-License-Identifier: AGPL-3.0-only
"""#5653: a completed canon sync is reused only while it is recent.

#5649 lets an `unsettled` runner settle its host gate on a completed result of
the same snapshot instead of paying a full sync. That result was reused at any
age: after a long stop of every runner on a host, the first one to restart
settled the gate on an arbitrarily old canon until the next periodic refresh.
The reuse is now bounded by `SYNC_REUSE_MAX_INTERVALS` x `canon_sync_interval`
(and a result whose completion time is unknown is never reused).
"""
import types
from unittest import mock

from ameesh import canon_sync
from .support import PgTestCase

HOST = "reuse5653"


class _Setup(PgTestCase):

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

    def run_sync(self, canon=None, unsettled=False):
        return canon_sync.periodic_sync(
            self.db, canon or self.canon, HOST, 300,
            on_incomplete=self.messages.append, unsettled=unsettled)

    def complete_ok(self):
        """A completed sync, as `sync()` leaves it: state `ok`."""
        report = self.run_sync()
        self.db.execute("UPDATE canon_state SET status = 'ok' "
                        "WHERE host = %s AND canon = ''", (HOST,))
        return report

    def age_completed(self, seconds):
        self.db.execute(
            "UPDATE canon_state SET sync_completed_at = "
            "clock_timestamp() - make_interval(secs => %s) "
            "WHERE host = %s AND canon = ''", (float(seconds), HOST))

    def expire_claim(self, seconds=301):
        self.db.execute(
            "UPDATE canon_state SET sync_claimed_at = "
            "clock_timestamp() - make_interval(secs => %s) "
            "WHERE host = %s AND canon = ''", (float(seconds), HOST))


class CompletedAgeTest(_Setup):

    def test_complete_sync_publishes_the_completion_age(self):
        with mock.patch.object(canon_sync, "sync"):
            self.complete_ok()
        shared = self.store.shared_sync(HOST, "")
        self.assertIsNotNone(shared)
        self.assertIsNotNone(shared["completed_age_s"])
        self.assertGreaterEqual(shared["completed_age_s"], 0.0)
        self.assertLess(shared["completed_age_s"], 30.0)


class ReuseAgeBoundTest(_Setup):
    """An unsettled runner reuses a fresh result and pays a full sync when the
    result is older than the bound."""

    def test_unsettled_runner_reuses_a_result_within_the_bound(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            # more than one interval apart: #5649's staging sequence case
            self.age_completed(2 * 300)
            self.expire_claim(601)
            report = self.run_sync(unsettled=True)
            self.assertTrue(report.shared)
            self.assertEqual(sync.call_count, 1)

    def test_unsettled_runner_syncs_again_when_the_result_is_too_old(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            self.age_completed(3 * 300 + 60)     # beyond 3 x interval
            self.expire_claim(3 * 300 + 60)
            report = self.run_sync(unsettled=True)
            self.assertIs(report, sync.return_value)
            self.assertEqual(sync.call_count, 2)   # no reuse: a full sync
            # the new completion is fresh again
            shared = self.store.shared_sync(HOST, "")
            self.assertLess(shared["completed_age_s"], 30.0)

    def test_unknown_completion_time_is_never_reused(self):
        # No usable date at all: neither a published completion nor the claim
        # that produced it (#5688 keeps that claim proxy; without either, the
        # result stays unusable until the next complete sync).
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            self.db.execute(
                "UPDATE canon_state SET sync_completed_at = NULL, "
                "sync_claimed_at = NULL WHERE host = %s AND canon = ''",
                (HOST,))
            self.assertIsNone(self.store.shared_sync(HOST, "")["completed_age_s"])
            report = self.run_sync(unsettled=True)
            self.assertIs(report, sync.return_value)
            self.assertEqual(sync.call_count, 2)


class StalePendingTest(_Setup):
    """A stale result never settles the gate while a newer claim is pending."""

    def test_stale_result_does_not_settle_a_pending_claim(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            self.age_completed(3 * 300 + 60)
            self.expire_claim()
            self.assertTrue(self.store.claim_sync(HOST, "", "neighbour", 300))
            report = self.run_sync()
            self.assertIsNone(report)
            self.assertEqual(sync.call_count, 1)
            self.assertIn("too old", self.messages[-1])
            self.assertIn("age", self.messages[-1])

    def test_pending_claim_still_reuses_a_fresh_result(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            self.expire_claim()
            self.assertTrue(self.store.claim_sync(HOST, "", "neighbour", 300))
            report = self.run_sync()
            self.assertTrue(report.shared)
            self.assertEqual(sync.call_count, 1)
