# SPDX-License-Identifier: AGPL-3.0-only
"""#5688: a sync completed by an old runner still dates the shared result.

During the staging sequence, runners still on the pre-0541 code keep claiming
and completing the shared sync of their host, but their `complete_sync` has no
`sync_completed_at`: the column keeps the 0541 backfill value and its age only
grows. The restarted runner then refuses the result as "too old", never takes
its lease, and misses the "redémarrer" guard (24 x 5 s).

`shared_sync` dates a completed result by the claim that produced it when that
claim is still the completed one — the same conservative proxy as the 0541
backfill. A pending claim never dates the previous result, and a host whose
runners all stopped keeps its old age, so #5653's host gate is not loosened.
"""
import types
from unittest import mock

from ameesh import canon_sync
from .support import PgTestCase

HOST = "mixed5688"
INTERVAL = 300
MAX_AGE = INTERVAL * canon_sync.SYNC_REUSE_MAX_INTERVALS


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
            self.db, canon or self.canon, HOST, INTERVAL,
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

    def age_claim(self, seconds):
        self.db.execute(
            "UPDATE canon_state SET sync_claimed_at = "
            "clock_timestamp() - make_interval(secs => %s) "
            "WHERE host = %s AND canon = ''", (float(seconds), HOST))

    def old_runner_completes(self, token="old-runner"):
        """A pre-0541 `complete_sync`: it advances the completed token but
        never writes `sync_completed_at`; `sync_claimed_at` stays the claim
        that produced the result."""
        self.db.execute(
            "UPDATE canon_state SET sync_token = %s, sync_completed_token = %s, "
            "sync_claimed_at = clock_timestamp() "
            "WHERE host = %s AND canon = ''", (token, token, HOST))


class OldRunnerRefreshTest(_Setup):
    """The claim of an old runner dates its completed result."""

    def test_old_runner_completion_refreshes_the_reuse_age(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            # the 0541 backfill value is the only completion time, now older
            # than the bound; the old runner's own claim is fresh
            self.age_completed(MAX_AGE + 60)
            self.old_runner_completes()
            shared = self.store.shared_sync(HOST, "")
            self.assertLess(shared["completed_age_s"], 30.0)
            report = self.run_sync(unsettled=True)
            self.assertTrue(report.shared)
            self.assertEqual(sync.call_count, 1)

    def test_old_runner_on_a_never_backfilled_row_is_reusable(self):
        # A row created after 0541 never got a completion timestamp; an old
        # runner completes it: its claim is the only usable date (#5688).
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            self.db.execute("UPDATE canon_state SET sync_completed_at = NULL "
                            "WHERE host = %s AND canon = ''", (HOST,))
            self.old_runner_completes()
            report = self.run_sync(unsettled=True)
            self.assertTrue(report.shared)
            self.assertEqual(sync.call_count, 1)


class HostGateStillBoundedTest(_Setup):
    """The old-runner proxy does not loosen #5653's host gate."""

    def test_a_long_stopped_host_still_refuses_the_result(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            self.old_runner_completes()
            # every runner stopped: the claim that produced the result aged
            self.age_completed(MAX_AGE + 60)
            self.age_claim(MAX_AGE + 60)
            report = self.run_sync(unsettled=True)
            self.assertIs(report, sync.return_value)
            self.assertEqual(sync.call_count, 2)
            shared = self.store.shared_sync(HOST, "")
            self.assertLess(shared["completed_age_s"], 30.0)

    def test_a_pending_claim_does_not_rejuvenate_the_old_result(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            self.age_completed(MAX_AGE + 60)
            self.age_claim(INTERVAL + 1)      # let a newer claim take over
            self.assertTrue(self.store.claim_sync(HOST, "", "pending",
                                                  INTERVAL))
            shared = self.store.shared_sync(HOST, "")
            self.assertGreater(shared["completed_age_s"], MAX_AGE)
            report = self.run_sync()
            self.assertIsNone(report)
            self.assertEqual(sync.call_count, 1)
            self.assertIn("too old", self.messages[-1])

    def test_a_pending_claim_does_not_date_a_previous_old_completion(self):
        # The previous completion came from an old runner too: its claim was
        # fresh then, but a newer pending claim must not extend its age.
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            self.old_runner_completes()
            self.age_completed(MAX_AGE + 60)
            self.age_claim(INTERVAL + 1)
            self.assertTrue(self.store.claim_sync(HOST, "", "pending",
                                                  INTERVAL))
            shared = self.store.shared_sync(HOST, "")
            self.assertGreater(shared["completed_age_s"], MAX_AGE)
            self.assertIsNone(self.run_sync())
            self.assertEqual(sync.call_count, 1)

    def test_unknown_completion_time_without_a_claim_is_never_reused(self):
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
