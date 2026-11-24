# SPDX-License-Identifier: AGPL-3.0-only
"""#8200 — un refus transitoire d'une sonde vérifiée par le serveur se lève.

Une ligne `failing` gardée (même empreinte) ne se lève que par la preuve d'une
credential neuve (#210 r4). Pour `gh` et `scw`, la sonde interroge le service à
chaque passe : deux verdicts sains espacés d'au moins quinze minutes sont la
preuve du serveur et lèvent le refus. `claude`, `codex` et `gcloud` gardent la
règle d'origine (présence locale, jeton peut-être en cache).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import sentinel
from ameesh.storage.postgres.credentials import Credentials
from tests.support import PgTestCase

NOW = 1_800_000_000.0
FAILED = sentinel._digest("x")
FRESH = sentinel._digest("y")


def _failing(harness="gh", account="", **extra):
    row = {"state": "failing", "source": "turn", "failures": 1,
           "failing_since_ts": NOW - 600, "checked_ts": NOW - 600,
           "last_failure_ts": NOW - 600, "failing_fingerprint": FAILED}
    row.update(extra)
    return {account: row}


def _check(verdict, current, *, streak=None, report=None, dry_run=False):
    creds = mock.Mock()
    creds.health.return_value = current
    creds.probe_streak.return_value = streak
    creds.report.return_value = report or {"state": "healthy", "previous": "failing"}
    cfg = SimpleNamespace(host="h1", accounts={}, credential_sentinel={})
    with mock.patch.object(sentinel, "probe_all", return_value=[verdict]), \
            mock.patch("ameesh.storage.of",
                       return_value=SimpleNamespace(credentials=creds)):
        rows = sentinel.check(cfg, object(), now=NOW, dry_run=dry_run)
    return rows, creds


class ServerVerifiedRecoveryTest(unittest.TestCase):
    """La série de sondes saines de `gh`/`scw` lève un échec gardé."""

    def test_gh_counts_a_spaced_healthy_probe_then_clears(self):
        verdict = sentinel.Verdict("gh", "", "healthy", "authenticated",
                                   fingerprint=FAILED)
        current = _failing("gh")
        # first healthy probe: the row stays failing, the series starts
        rows, creds = _check(verdict, current, streak=1)
        creds.probe_streak.assert_called_once_with(
            "h1", "gh", "", healthy=True,
            interval=sentinel.SERVER_HEAL_INTERVAL_S, expect_failures=1)
        creds.report.assert_not_called()
        creds.touch.assert_not_called()
        self.assertEqual(rows[0]["state"], "failing")
        self.assertIn("no new credential", rows[0]["kept"])
        self.assertIn("server-verified healthy probe 1/2", rows[0]["kept"])
        # second one, fifteen minutes later: the server's proof lands
        rows, creds = _check(verdict, current, streak=2)
        self.assertEqual(creds.report.call_args[0], ("h1", "gh", "", "healthy"))
        self.assertEqual(creds.report.call_args[1]["expect_failures"], 1)
        self.assertEqual(creds.report.call_args[1]["fingerprint"], FAILED)
        creds.touch.assert_not_called()
        self.assertEqual((rows[0]["state"], rows[0]["previous"]), ("healthy", "failing"))

    def test_scw_counts_and_clears_the_same_way(self):
        verdict = sentinel.Verdict("scw", "", "healthy", "authenticated",
                                   fingerprint=FAILED)
        _rows, creds = _check(verdict, _failing("scw"), streak=2)
        creds.probe_streak.assert_called_once_with(
            "h1", "scw", "", healthy=True,
            interval=sentinel.SERVER_HEAL_INTERVAL_S, expect_failures=1)
        self.assertEqual(creds.report.call_args[0][3], "healthy")

    def test_an_unknown_probe_breaks_the_series(self):
        verdict = sentinel.Verdict("gh", "", "unknown", "probe failed: Timeout")
        rows, creds = _check(verdict, _failing("gh"), streak=0)
        creds.probe_streak.assert_called_once_with(
            "h1", "gh", "", healthy=False,
            interval=sentinel.SERVER_HEAL_INTERVAL_S, expect_failures=1)
        creds.report.assert_not_called()
        creds.touch.assert_not_called()
        self.assertEqual(rows[0]["state"], "failing")

    def test_a_fresh_credential_still_clears_immediately(self):
        verdict = sentinel.Verdict("gh", "", "healthy", "authenticated",
                                   fingerprint=FRESH)
        rows, creds = _check(verdict, _failing("gh"))
        creds.probe_streak.assert_not_called()
        creds.touch.assert_not_called()
        self.assertEqual(creds.report.call_args[0][3], "healthy")
        self.assertEqual(rows[0]["state"], "healthy")

    def test_a_failure_recorded_during_the_probe_is_not_overwritten(self):
        verdict = sentinel.Verdict("gh", "", "healthy", "authenticated",
                                   fingerprint=FAILED)
        rows, creds = _check(verdict, _failing("gh"), streak=2,
                             report={"skipped": True})
        creds.report.assert_called_once()
        self.assertEqual(rows[0]["state"], "failing")
        self.assertIn("recorded during the probe", rows[0]["kept"])

    def test_dry_run_writes_nothing(self):
        verdict = sentinel.Verdict("gh", "", "healthy", "authenticated",
                                   fingerprint=FAILED)
        _rows, creds = _check(verdict, _failing("gh"), streak=2, dry_run=True)
        creds.probe_streak.assert_not_called()
        creds.touch.assert_not_called()
        creds.report.assert_not_called()


class LocalPresenceKeepsTheGuardTest(unittest.TestCase):
    """#210 r4 reste la règle pour claude, codex et gcloud."""

    def test_a_healthy_local_presence_never_clears_a_kept_failure(self):
        # même empreinte : la sonde ne prouve pas une credential neuve
        verdict = sentinel.Verdict("claude", "a", "healthy", "logged in",
                                   fingerprint=FAILED)
        rows, creds = _check(verdict, _failing("claude", "a"), streak=99)
        creds.probe_streak.assert_not_called()
        creds.report.assert_not_called()
        creds.touch.assert_called_once_with("h1", "claude", "a", expect_failures=1)
        self.assertEqual(rows[0]["state"], "failing")
        self.assertIn("no new credential", rows[0]["kept"])

    def test_codex_and_gcloud_are_not_server_verified(self):
        for harness in ("codex", "gcloud"):
            with self.subTest(harness=harness):
                verdict = sentinel.Verdict(harness, "", "healthy", "ok",
                                           fingerprint=FAILED)
                _rows, creds = _check(verdict, _failing(harness), streak=99)
                creds.probe_streak.assert_not_called()
                creds.report.assert_not_called()
                self.assertTrue(creds.touch.called)

    def test_the_server_verified_set_is_exactly_gh_and_scw(self):
        self.assertEqual(set(sentinel.SERVER_VERIFIED), {"gh", "scw"})
        self.assertGreaterEqual(sentinel.SERVER_HEAL_PROBES, 2)
        self.assertGreaterEqual(sentinel.SERVER_HEAL_INTERVAL_S, 900)


class ProbeStreakStorageTest(PgTestCase):
    """La série est persistée et ne traverse ni un nouvel échec ni une course."""

    HOST = "h1"
    HARNESS = "gh"
    ACCOUNT = ""

    def setUp(self):
        super().setUp()
        self.db.execute("delete from credential_health")
        self.creds = Credentials(self.db)

    def _fail(self, fingerprint="x"):
        self.creds.report(self.HOST, self.HARNESS, self.ACCOUNT, "failing",
                          reason="bad credentials", source="turn",
                          fingerprint=fingerprint)

    def _row(self):
        return self.db.query(
            "select state, failures, healthy_streak,"
            " extract(epoch from healthy_probe_at)::float8 as healthy_ts"
            " from credential_health where host = %s and harness = %s"
            " and account = %s", (self.HOST, self.HARNESS, self.ACCOUNT))[0]

    def _anchor(self, *, probe_seconds, failure_seconds):
        """Place l'ancre et le dernier échec dans le passé (jamais l'inverse :
        une ancre antérieure au dernier échec est la garde N-1)."""
        self.db.execute(
            "update credential_health set"
            " healthy_probe_at = now() - make_interval(secs => %s),"
            " last_failure_at = now() - make_interval(secs => %s)"
            " where host = %s and harness = %s and account = %s",
            (probe_seconds, failure_seconds, self.HOST, self.HARNESS, self.ACCOUNT))

    def _streak(self, *, healthy=True, expect_failures=1, interval=900):
        return self.creds.probe_streak(
            self.HOST, self.HARNESS, self.ACCOUNT, healthy=healthy,
            interval=interval, expect_failures=expect_failures)

    def test_two_spaced_healthy_probes_then_the_clear(self):
        self._fail()
        self.assertEqual(self._streak(), 1)
        row = self._row()
        self.assertEqual((row["state"], row["healthy_streak"]), ("failing", 1))
        # a probe closer than the interval does not count twice
        self.assertEqual(self._streak(), 1)
        self.assertEqual(self._row()["healthy_streak"], 1)
        # fifteen minutes after the counted probe: the second one lands
        self._anchor(probe_seconds=1200, failure_seconds=1800)
        self.assertEqual(self._streak(), 2)
        # the sentinel then writes the clearing verdict as usual
        self.creds.report(self.HOST, self.HARNESS, self.ACCOUNT, "healthy",
                          reason="authenticated", source="sentinel",
                          expect_failures=1)
        row = self._row()
        self.assertEqual(row["state"], "healthy")
        self.assertEqual(row["healthy_streak"], 0)
        self.assertIsNone(row["healthy_ts"])

    def test_a_non_healthy_probe_breaks_the_series(self):
        self._fail()
        self.assertEqual(self._streak(), 1)
        self.assertEqual(self._streak(healthy=False), 0)
        row = self._row()
        self.assertEqual(row["healthy_streak"], 0)
        self.assertIsNone(row["healthy_ts"])
        self.assertEqual(self._streak(), 1)

    def test_a_new_failure_restarts_the_series(self):
        self._fail()
        self.assertEqual(self._streak(), 1)
        # a failure written by an N-1 writer: it does not know the columns, so
        # it must still break the series through `last_failure_at`. The anchor
        # is old enough to pass the spacing; only the newer failure resets.
        self.db.execute(
            "update credential_health set state = 'failing', reason = 'x',"
            " source = 'turn', checked_at = now(), last_failure_at = now(),"
            " failures = failures + 1"
            " where host = %s and harness = %s and account = %s",
            (self.HOST, self.HARNESS, self.ACCOUNT))
        self._anchor(probe_seconds=1200, failure_seconds=600)
        self.assertEqual(self._streak(expect_failures=2), 1)
        self.assertEqual(self._row()["healthy_streak"], 1)

    def test_a_failure_counted_meanwhile_writes_nothing(self):
        self._fail()
        self.assertEqual(self._streak(expect_failures=0), None)
        row = self._row()
        self.assertEqual((row["state"], row["healthy_streak"]), ("failing", 0))


if __name__ == "__main__":
    unittest.main()
