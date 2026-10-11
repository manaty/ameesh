# SPDX-License-Identifier: AGPL-3.0-only
"""L226a — credential sentinel: probes, reports and owner alerts.

Pure tests: credential files are written in a temporary directory, commands
are faked, the storage is a stub. No real `~/.claude`, gcloud or database.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import accounts, exploitation, notify, sentinel

NOW = 1_800_000_000.0


def _claude_dir(root: str, name: str, **oauth) -> str:
    path = os.path.join(root, name)
    os.makedirs(path, mode=0o700)
    with open(os.path.join(path, ".credentials.json"), "w", encoding="utf-8") as fh:
        json.dump({"claudeAiOauth": oauth}, fh)
    return path


class ProbeClaudeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l226a-")
        which = mock.patch.object(sentinel.shutil, "which", return_value="/bin/claude")
        which.start()
        self.addCleanup(which.stop)

    def _profile(self, name, **oauth):
        return accounts.Profile("claude", name, path=_claude_dir(self.tmp, name, **oauth))

    @staticmethod
    def _status(logged_in=True, code=0):
        calls = []

        def run(argv, env=None, whole=False):
            calls.append((argv, env))
            return code, json.dumps({"loggedIn": logged_in, "authMethod": "claude.ai"})
        run.calls = calls
        return run

    def test_expired_access_token_is_healthy_when_logged_in(self):
        # bureau 2026-10-11: expiresAt passes after a short idle time; the
        # access token is refreshed on use — not a failure
        profile = self._profile("a", refreshToken="x", expiresAt=(NOW - 1800) * 1000,
                                refreshTokenExpiresAt=(NOW + 86400) * 1000)
        run = self._status()
        v = sentinel.probe_claude(profile, NOW, run=run)
        self.assertEqual((v.state, v.account), ("healthy", "a"))
        self.assertAlmostEqual(v.expires_at, NOW + 86400)
        argv, env = run.calls[0]
        self.assertEqual(argv, ["claude", "auth", "status"])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], profile.path)

    def test_not_logged_in_fails_with_exact_command(self):
        profile = self._profile("b", refreshToken="x")
        v = sentinel.probe_claude(profile, NOW, run=self._status(logged_in=False, code=1))
        self.assertEqual(v.state, "failing")
        self.assertIn("CLAUDE_CONFIG_DIR=%s claude auth login" % profile.path, v.text())

    def test_expired_refresh_token_fails(self):
        profile = self._profile("c", refreshToken="x", refreshTokenExpiresAt=(NOW - 1) * 1000)
        self.assertEqual(sentinel.probe_claude(profile, NOW, run=self._status()).state,
                         "failing")

    def test_default_credential_absent_is_skipped(self):
        profile = accounts.Profile("claude", "")
        self.assertIsNone(sentinel.probe_claude(
            profile, NOW, environ={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "none")},
            run=self._status()))

    def test_timeout_or_garbage_is_unknown_not_a_verdict(self):
        profile = self._profile("d", refreshToken="x")
        timeout = mock.Mock(return_value=(None, "TimeoutExpired"))
        garbage = mock.Mock(return_value=(1, "not json"))
        self.assertEqual(sentinel.probe_claude(profile, NOW, run=timeout).state, "unknown")
        self.assertEqual(sentinel.probe_claude(profile, NOW, run=garbage).state, "unknown")


class ProbeCodexTest(unittest.TestCase):
    def _profile(self, auth):
        path = tempfile.mkdtemp(prefix="ameesh-l226a-codex-")
        with open(os.path.join(path, "auth.json"), "w", encoding="utf-8") as fh:
            json.dump(auth, fh)
        return accounts.Profile("codex", "a", path=path)

    def test_refresh_token_is_healthy(self):
        v = sentinel.probe_codex(self._profile({"tokens": {"refresh_token": "x"}}), NOW)
        self.assertEqual(v.state, "healthy")

    def test_no_token_fails_with_codex_login(self):
        profile = self._profile({"tokens": {}})
        v = sentinel.probe_codex(profile, NOW)
        self.assertEqual(v.state, "failing")
        self.assertIn("CODEX_HOME=%s codex login" % profile.path, v.text())


class ProbeCommandsTest(unittest.TestCase):
    conf = {"gcloud_session_hours": 16.0}

    def test_gcloud_failure_carries_reauth_command(self):
        run = mock.Mock(return_value=(1, "ERROR: Reauthentication failed."))
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/gcloud"):
            v = sentinel.probe_gcloud(self.conf, NOW, run, environ={"CLOUDSDK_CONFIG": "/x"})
        self.assertEqual(v.state, "failing")
        self.assertIn("Reauthentication failed", v.reason)
        self.assertIn("gcloud auth login --update-adc", v.text())

    def test_gcloud_forecast_is_last_login_plus_session_length(self):
        tmp = tempfile.mkdtemp(prefix="ameesh-l226a-gcloud-")
        db = os.path.join(tmp, "credentials.db")
        open(db, "w").close()
        os.utime(db, (NOW - 3600, NOW - 3600))
        run = mock.Mock(return_value=(0, "ya29..."))
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/gcloud"):
            v = sentinel.probe_gcloud(self.conf, NOW, run, environ={"CLOUDSDK_CONFIG": tmp})
        self.assertEqual(v.state, "healthy")
        self.assertAlmostEqual(v.expires_at, NOW - 3600 + 16 * 3600)

    def test_timeout_is_unknown(self):
        run = mock.Mock(return_value=(None, "TimeoutExpired"))
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/gh"):
            v = sentinel.probe_cli("gh", ["gh", "auth", "status"], "gh auth login", run)
        self.assertEqual(v.state, "unknown")

    def test_missing_binary_is_skipped(self):
        with mock.patch.object(sentinel.shutil, "which", return_value=None):
            self.assertIsNone(sentinel.probe_cli("scw", ["scw"], "scw init"))
            self.assertIsNone(sentinel.probe_gcloud(self.conf, NOW))


class CheckReportsTest(unittest.TestCase):
    def test_each_verdict_is_reported_by_the_sentinel(self):
        cfg = SimpleNamespace(host="h1", accounts={}, credential_sentinel={"probes": ["gh"]})
        creds = mock.Mock()
        creds.report.return_value = {"state": "failing", "previous": "healthy"}
        run = mock.Mock(return_value=(1, "not logged in"))
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/gh"), \
                mock.patch("ameesh.storage.of", return_value=SimpleNamespace(credentials=creds)):
            rows = sentinel.check(cfg, object(), now=NOW, run=run)
        creds.report.assert_called_once()
        args, kw = creds.report.call_args
        self.assertEqual(args, ("h1", "gh", "", "failing"))
        self.assertEqual(kw["source"], "sentinel")
        self.assertIn("gh auth login -h github.com", kw["reason"])
        self.assertEqual(rows[0]["previous"], "healthy")


class AlertsTest(unittest.TestCase):
    def _alerts(self, rows):
        creds = mock.Mock()
        creds.listing.return_value = rows
        with mock.patch.object(exploitation.storage, "of",
                               return_value=SimpleNamespace(credentials=creds)):
            return exploitation._credentials(object(), NOW)

    def _row(self, **kw):
        row = {"host": "h1", "harness": "gcloud", "account": "", "state": "healthy",
               "reason": "", "source": "sentinel", "checked_ts": NOW - 60,
               "failing_since_ts": None, "expires_ts": None}
        row.update(kw)
        return row

    def test_failing_credential_alerts_with_fix_command(self):
        [alert] = self._alerts([self._row(state="failing", failing_since_ts=NOW - 600,
                                          reason="expired; fix: gcloud auth login")])
        self.assertEqual(alert["type"], "credential_failing")
        self.assertIn("gcloud auth login", alert["detail"])
        self.assertEqual((alert["host"], alert["harness"]), ("h1", "gcloud"))

    def test_forecast_expiry_warns_before_it_happens(self):
        [alert] = self._alerts([self._row(expires_ts=NOW + 3 * 3600)])
        self.assertEqual(alert["type"], "credential_expiring")
        self.assertEqual(self._alerts([self._row(expires_ts=NOW + 48 * 3600)]), [])

    def test_two_accounts_are_two_alerts(self):
        rows = [self._row(harness="claude", account=n, state="failing",
                          failing_since_ts=NOW - 600) for n in ("a", "b")]
        keys = {exploitation.alert_key(a) for a in self._alerts(rows)}
        self.assertEqual(len(keys), 2)

    def test_failing_is_pushed_and_urgent_by_default(self):
        self.assertIn("credential_failing", notify.DEFAULT_TYPES)
        self.assertIn("credential_expiring", notify.DEFAULT_TYPES)
        self.assertIn("credential_failing", notify.URGENT_TYPES)
        for kind in ("credential_failing", "credential_expiring"):
            self.assertIn(kind, exploitation.ALERT_TYPES)
            self.assertIn(kind, notify.TYPE_LABELS)


if __name__ == "__main__":
    unittest.main()
