# SPDX-License-Identifier: AGPL-3.0-only
"""#5688: a sync completed by an old runner keeps dating the shared result.

During the staging sequence, runners still on the pre-0541 code keep claiming
and completing the shared sync of their host, but their `complete_sync` has no
`sync_completed_at`: the column keeps the 0541 backfill value and its age only
grows. The restarted runner then refuses the result as "too old", never takes
its lease, and misses the "redémarrer" guard (24 x 5 s).

Migration 0542 keeps the claim that produced the last completion in
`sync_completed_claim_at`, whatever the writer: a following claim (old runner
included) overwrites `sync_claimed_at` but not that proof, so a pending claim
never hides a recent completion (#5493) nor dates a previous result. A host
whose runners all stopped keeps its old age, so #5653's host gate is not
loosened.
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
        """The whole completion aged: published time and producing claim."""
        self.db.execute(
            "UPDATE canon_state SET sync_completed_at = "
            "clock_timestamp() - make_interval(secs => %s), "
            "sync_completed_claim_at = "
            "clock_timestamp() - make_interval(secs => %s) "
            "WHERE host = %s AND canon = ''",
            (float(seconds), float(seconds), HOST))

    def age_completed_claim(self, seconds):
        self.db.execute(
            "UPDATE canon_state SET sync_completed_claim_at = "
            "clock_timestamp() - make_interval(secs => %s) "
            "WHERE host = %s AND canon = ''", (float(seconds), HOST))

    def age_claim(self, seconds):
        self.db.execute(
            "UPDATE canon_state SET sync_claimed_at = "
            "clock_timestamp() - make_interval(secs => %s) "
            "WHERE host = %s AND canon = ''", (float(seconds), HOST))

    def old_runner_completes(self):
        """A pre-0541 `complete_sync`: it advances the completed token and
        leaves `sync_completed_at` untouched; the 0542 trigger records the
        claim that produced the result."""
        self.db.execute(
            "UPDATE canon_state SET sync_token = 'old-runner', "
            "sync_completed_token = 'old-runner', "
            "sync_claimed_at = clock_timestamp() "
            "WHERE host = %s AND canon = ''", (HOST,))


class OldRunnerRefreshTest(_Setup):
    """The completion claim kept by 0542 dates an old runner's result."""

    def test_old_runner_completion_refreshes_the_reuse_age(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            # the 0541 backfill value is the only published time, now older
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
        # runner completes it: the 0542 claim is the only usable date.
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            self.db.execute("UPDATE canon_state SET sync_completed_at = NULL "
                            "WHERE host = %s AND canon = ''", (HOST,))
            self.old_runner_completes()
            report = self.run_sync(unsettled=True)
            self.assertTrue(report.shared)
            self.assertEqual(sync.call_count, 1)

    def test_a_pending_claim_keeps_the_last_completion_date(self):
        # Reviewer case (#188844): completion claim at -301 s, published time
        # at -3600 s, then a newer claim pending. The age must stay the
        # completion's 301 s, not fall back to 3600 s (#5493 reuse).
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            self.age_completed(3600)
            self.old_runner_completes()
            self.age_completed_claim(301)
            self.age_claim(301)
            self.assertTrue(self.store.claim_sync(HOST, "", "pending",
                                                  INTERVAL))
            shared = self.store.shared_sync(HOST, "")
            self.assertGreater(shared["completed_age_s"], 250)
            self.assertLess(shared["completed_age_s"], MAX_AGE)
            report = self.run_sync()
            self.assertTrue(report.shared)
            self.assertEqual(sync.call_count, 1)


class HostGateStillBoundedTest(_Setup):
    """The 0542 completion claim does not loosen #5653's host gate."""

    def test_a_long_stopped_host_still_refuses_the_result(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            self.old_runner_completes()
            # every runner stopped: the completion and its claim aged
            self.age_completed(MAX_AGE + 60)
            self.age_claim(MAX_AGE + 60)
            report = self.run_sync(unsettled=True)
            self.assertIs(report, sync.return_value)
            self.assertEqual(sync.call_count, 2)
            shared = self.store.shared_sync(HOST, "")
            self.assertLess(shared["completed_age_s"], 30.0)

    def test_a_pending_claim_does_not_date_a_genuinely_old_result(self):
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

    def test_unknown_completion_time_without_a_claim_is_never_reused(self):
        with mock.patch.object(canon_sync, "sync") as sync:
            self.complete_ok()
            self.db.execute(
                "UPDATE canon_state SET sync_completed_at = NULL, "
                "sync_completed_claim_at = NULL, sync_claimed_at = NULL "
                "WHERE host = %s AND canon = ''", (HOST,))
            self.assertIsNone(self.store.shared_sync(HOST, "")["completed_age_s"])
            report = self.run_sync(unsettled=True)
            self.assertIs(report, sync.return_value)
            self.assertEqual(sync.call_count, 2)
