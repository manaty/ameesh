# SPDX-License-Identifier: AGPL-3.0-only
"""#5649: a staging restart must settle the host gate on the last COMPLETED
canon sync of the same snapshot instead of starting a new full sync.

The sequence restarts the units one by one, more than one `canon_sync_interval`
apart. `periodic_sync` claimed first, so every restarted runner ran a full
sync (minutes in staging) before claiming its lease, and the "redémarrer"
guard (24 x 5 s) failed. A failed claimed sync also withdrew the last
completed result, so the next restarted unit had no result to reuse.
"""
import types
from unittest import mock

from ameesh import canon_sync
from .support import PgTestCase

HOST = "startup5649"


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

    def expire_claim(self):
        self.db.execute("UPDATE canon_state SET sync_claimed_at = "
                        "clock_timestamp() - interval '301 seconds' WHERE host = %s",
                        (HOST,))

    def run_ok_sync(self, canon=None):
        """A completed sync, as `sync()` leaves it: state `ok`."""
        report = self.run_sync(canon)
        self.db.execute("UPDATE canon_state SET status = 'ok' "
                        "WHERE host = %s AND canon = ''", (HOST,))
        return report


class StartupReuseTest(_Setup):
    """A runner without a host gate yet must not start a new sync when the
    last completed one covers the same immutable sources and snapshot."""

    def test_unsettled_runner_reuses_matching_completed_result_before_claiming(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.run_ok_sync()                 # one completed sync
            self.expire_claim()                # the sequence restarts us later
            report = self.run_sync(unsettled=True)
            self.assertTrue(report.shared)
            self.assertEqual(sync.call_count, 1)   # no new full sync
            self.assertEqual(self.messages, [])

    def test_unsettled_runner_still_takes_over_a_different_snapshot(self):
        # #5493 stays: a different snapshot is never reused.
        changed = types.SimpleNamespace(**vars(self.canon))
        changed.load_findings = ["finding of another version"]
        with mock.patch.object(canon_sync, "sync") as sync:
            self.run_ok_sync()
            report = self.run_sync(changed, unsettled=True)
            self.assertIs(report, sync.return_value)
            self.assertEqual(sync.call_count, 2)

    def test_settled_runner_keeps_the_interval_pacing(self):
        # No regression on the periodic refresh: a settled runner still waits
        # for the interval and reuses the completed result meanwhile.
        with mock.patch.object(canon_sync, "sync") as sync:
            self.run_ok_sync()
            report = self.run_sync()
            self.assertTrue(report.shared)
            self.assertEqual(sync.call_count, 1)
            self.expire_claim()
            self.assertIs(self.run_sync(), sync.return_value)
            self.assertEqual(sync.call_count, 2)


class FailedClaimKeepsCompletedTest(_Setup):
    """A failed claimed sync releases its reservation but no longer withdraws
    the last completed result when the canon state is still `ok`; the
    signature check already refuses a result of another snapshot."""

    def test_failed_claimed_sync_keeps_last_completed_result(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.run_ok_sync()
            self.expire_claim()
            sync.side_effect = RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                self.run_sync()
            self.assertIsNotNone(self.store.shared_sync(HOST, ""))
            sync.side_effect = None
            report = self.run_sync(unsettled=True)
            self.assertTrue(report.shared)
            self.assertEqual(sync.call_count, 2)   # no retry storm

    def test_failed_claimed_sync_releases_its_claim(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.run_ok_sync()
            self.expire_claim()
            sync.side_effect = RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                self.run_sync()
            self.assertTrue(self.store.claim_sync(HOST, "", "next", 300))

    def test_a_failure_that_records_a_non_ok_state_withdraws_the_result(self):
        # An unreadable canon records its state before raising; the last
        # completed result must not settle the gate over it.
        with mock.patch.object(canon_sync, "sync") as sync:
            self.run_ok_sync()
            self.expire_claim()
            def fail_and_close(db, canon, host):
                db.execute("UPDATE canon_state SET status = 'unreadable' "
                           "WHERE host = %s AND canon = ''", (HOST,))
                raise canon_sync.CanonUnreadable(host, "unreadable", [])
            sync.side_effect = fail_and_close
            with self.assertRaises(canon_sync.CanonUnreadable):
                self.run_sync()
            self.assertIsNone(self.store.shared_sync(HOST, ""))
