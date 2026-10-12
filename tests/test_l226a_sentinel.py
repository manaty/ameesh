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
        creds.health.return_value = {}
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


class ReviewR1Test(unittest.TestCase):
    """#210 review r1 (codex, head 053a307): one test per finding."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l226a-r1-")

    def _codex(self, auth, mtime=None, name="a"):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        auth_file = os.path.join(path, "auth.json")
        with open(auth_file, "w", encoding="utf-8") as fh:
            json.dump(auth, fh)
        if mtime is not None:
            os.utime(auth_file, (mtime, mtime))
        return accounts.Profile("codex", name, path=path)

    def _check(self, current, verdicts):
        cfg = SimpleNamespace(host="h1", accounts={}, credential_sentinel={})
        creds = mock.Mock()
        creds.health.return_value = current
        creds.report.return_value = {"state": "x", "previous": "failing"}
        with mock.patch.object(sentinel, "probe_all", return_value=verdicts), \
                mock.patch("ameesh.storage.of", return_value=SimpleNamespace(credentials=creds)):
            return sentinel.check(cfg, object(), now=NOW), creds

    def test_p2_failure_cleared_only_by_a_fresh_login(self):
        # a turn found the Codex refresh token revoked at NOW - 600; the token
        # is still in auth.json (written before): the probe reads healthy
        failing = {"a": {"state": "failing", "source": "turn", "failures": 1,
                         "failing_since_ts": NOW - 600, "checked_ts": NOW - 600,
                         "last_failure_ts": NOW - 600,
                         "failing_fingerprint": sentinel._digest("x")}}
        stale = sentinel.probe_codex(self._codex({"tokens": {"refresh_token": "x"}},
                                                 mtime=NOW - 3600), NOW)
        self.assertEqual(stale.state, "healthy")
        rows, creds = self._check(failing, [stale])
        creds.report.assert_not_called()
        self.assertEqual(rows[0]["state"], "failing")
        self.assertIn("no new credential since the last failure", rows[0]["kept"])
        self.assertIn("ameesh accounts healthy codex a", rows[0]["kept"])
        # a timeout (unknown) does not clear it either
        rows, creds = self._check(failing, [sentinel.Verdict("codex", "a", "unknown",
                                                             "probe failed: Timeout")])
        creds.report.assert_not_called()
        # `codex login` rewrote auth.json after the failure: cleared
        fresh = sentinel.probe_codex(self._codex({"tokens": {"refresh_token": "y"}},
                                                 mtime=NOW - 60, name="b"), NOW)
        rows, creds = self._check({"b": failing["a"]}, [fresh])
        creds.report.assert_called_once()
        self.assertEqual(creds.report.call_args[0][3], "healthy")
        self.assertEqual(creds.report.call_args[1]["expect_failures"], 1)
        # a new failure, and a healthy credential that was not failing, are written
        rows, creds = self._check({}, [stale])
        creds.report.assert_called_once()

    def _failing(self, token, last, since=None):
        return {"state": "failing", "source": "turn", "failures": 2,
                "failing_since_ts": since or last, "checked_ts": last,
                "last_failure_ts": last, "failing_fingerprint": sentinel._digest(token)}

    def test_r2_same_token_rewritten_is_no_proof(self):
        # codex r2 (a): auth.json rewritten after the failure with the same
        # revoked refresh token — a newer mtime proves nothing
        same = sentinel.probe_codex(self._codex({"tokens": {"refresh_token": "x"}},
                                                mtime=NOW - 60), NOW)
        self.assertEqual(same.state, "healthy")
        rows, creds = self._check({"a": self._failing("x", NOW - 600)}, [same])
        creds.report.assert_not_called()
        self.assertEqual(rows[0]["state"], "failing")
        # a failure recorded without a fingerprint: no proof either
        rows, creds = self._check({"a": dict(self._failing("x", NOW - 600),
                                             failing_fingerprint=None)}, [same])
        creds.report.assert_not_called()
        # nor a healthy probe that cannot fingerprint the credential
        rows, creds = self._check({"a": self._failing("x", NOW - 600)},
                                  [sentinel.Verdict("codex", "a", "healthy", "ok")])
        creds.report.assert_not_called()

    def test_r2_compared_with_the_last_failure(self):
        # codex r2 (b): failure at t100 (token A), login at t200 (token B),
        # another authentication failure at t300 with token B: still failing
        t100, t200, t300 = NOW - 900, NOW - 800, NOW - 700
        relogged = sentinel.probe_codex(self._codex({"tokens": {"refresh_token": "B"}},
                                                    mtime=t200), NOW)
        rows, creds = self._check({"a": self._failing("B", t300, since=t100)}, [relogged])
        creds.report.assert_not_called()
        self.assertEqual(rows[0]["state"], "failing")
        # codex r4 (1): a different token written BEFORE the last failure —
        # a turn started with A, the owner installed B at t200, A failed at
        # t300 — B is not the token that failed: cleared
        rows, creds = self._check({"a": self._failing("A", t300, since=t100)}, [relogged])
        self.assertEqual(creds.report.call_args[0][3], "healthy")
        # a different token written after the last failure: cleared
        fresh = sentinel.probe_codex(self._codex({"tokens": {"refresh_token": "C"}},
                                                 mtime=NOW - 60, name="b"), NOW)
        rows, creds = self._check({"b": self._failing("B", t300, since=t100)}, [fresh])
        self.assertEqual(creds.report.call_args[0][3], "healthy")

    def test_r4_launch_fingerprint_is_the_key_handed_to_the_child(self):
        # codex r4 (2): the key file is replaced after the account was applied
        # to the child's environment — the child runs with A
        key = os.path.join(self.tmp, "k.key")
        with open(key, "w", encoding="utf-8") as fh:
            fh.write("key-A\n")
        os.chmod(key, 0o600)
        profile = accounts.Profile("deepseek", "k", kind="api_key_env", key_file=key,
                                   env="DEEPSEEK_API_KEY")
        env = {}
        accounts.apply_env(env, profile, {})
        with open(key, "w", encoding="utf-8") as fh:
            fh.write("key-B")
        self.assertEqual(sentinel.launch_fingerprint("deepseek", profile, env),
                         sentinel._digest("key-A"))
        self.assertEqual(sentinel.fingerprint("deepseek", profile), sentinel._digest("key-B"))
        # an OAuth account: the store the child's environment points to
        claude = accounts.Profile("claude", "c", path=_claude_dir(self.tmp, "c",
                                                                  refreshToken="rt"))
        self.assertEqual(sentinel.launch_fingerprint("claude", claude, {}),
                         sentinel._digest("rt"))

    def test_r4_transient_cli_failure_is_unknown(self):
        # codex r4 (5): scw during a DNS outage, gh timing out — unknown; a
        # refused credential is still failing
        cases = [("scw", "Error: dial tcp: lookup api.scaleway.com: no such host", "unknown"),
                 ("gh", "X Timeout trying to log in to github.com account a (default)",
                  "unknown"),
                 ("gh", "github.com\n  X Failed to log in to github.com account a (default)\n"
                        "  - The token in default is invalid.\n"
                        "  - To re-authenticate, run: gh auth login -h github.com", "failing"),
                 ("scw", "Error: scaleway-sdk-go: http error 401 Unauthorized: "
                         "authentication is denied", "failing"),
                 ("scw", "Error: something unexpected", "unknown")]
        for harness, output, state in cases:
            with self.subTest(output=output[:40]), \
                    mock.patch.object(sentinel.shutil, "which", return_value="/bin/x"):
                v = sentinel.probe_cli(harness, [harness], "fix it",
                                       mock.Mock(return_value=(1, output)))
                self.assertEqual(v.state, state)
        gcloud = ("ERROR: There was a problem refreshing your current auth tokens: "
                  "Max retries exceeded (Temporary failure in name resolution)\n"
                  "Please run:\n  $ gcloud auth login")
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/gcloud"):
            v = sentinel.probe_gcloud({"gcloud_session_hours": 16.0}, NOW,
                                      mock.Mock(return_value=(1, gcloud)),
                                      environ={"CLOUDSDK_CONFIG": self.tmp})
        self.assertEqual(v.state, "unknown")

    def test_r5_whole_output_is_classified(self):
        # codex r5: the refusal comes first, then over 4,000 characters of
        # other account statuses
        output = ("github.com\n  X Failed to log in to github.com account a (default)\n"
                  "  - The token in GH_TOKEN is invalid.\n"
                  + "\n".join("  ✓ Logged in to ghe%d.example account b%d" % (i, i)
                              for i in range(200)))
        self.assertGreater(len(output), 4000)
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/gh"):
            v = sentinel.probe_cli("gh", ["gh"], "fix", mock.Mock(return_value=(1, output)))
        self.assertEqual(v.state, "failing")

    def test_r5_gh_token_in_effect_is_fingerprinted(self):
        # codex r5: GH_TOKEN overrides hosts.yml — a refused GH_TOKEN=A then a
        # healthy B must differ, with or without a store
        store = os.path.join(self.tmp, "hosts.yml")
        for exists in (False, True):
            if exists:
                with open(store, "w", encoding="utf-8") as fh:
                    fh.write("github.com: {}\n")
            a = sentinel.env_fingerprint({"GH_TOKEN": "A"}, sentinel.GH_TOKEN_VARS, store)
            b = sentinel.env_fingerprint({"GH_TOKEN": "B\n"}, sentinel.GH_TOKEN_VARS, store)
            self.assertEqual((a, b), (sentinel._digest("A"), sentinel._digest("B")))
        self.assertEqual(sentinel.env_fingerprint({"GITHUB_TOKEN": "C"},
                                                  sentinel.GH_TOKEN_VARS, store),
                         sentinel._digest("C"))
        self.assertEqual(sentinel.env_fingerprint({}, sentinel.GH_TOKEN_VARS, store),
                         sentinel.file_fingerprint(store))
        cfg = SimpleNamespace(host="h1", accounts={}, credential_sentinel={"probes": ["gh"]})
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/gh"):
            found = sentinel.probe_all(cfg, NOW, mock.Mock(return_value=(0, "ok")),
                                       environ={"GH_TOKEN": "B", "GH_CONFIG_DIR": self.tmp})
        self.assertEqual(found[0].fingerprint, sentinel._digest("B"))

    def test_r4_turn_failure_gets_the_fix_command(self):
        # codex r4 (6): a turn recorded `invalid api key`; the probe keeps the
        # failure (same token) and adds the account's login command
        failing = {"a": dict(self._failing("x", NOW - 600), reason="invalid api key")}
        same = sentinel.probe_codex(self._codex({"tokens": {"refresh_token": "x"}}), NOW)
        rows, creds = self._check(failing, [same])
        creds.report.assert_not_called()
        path = os.path.join(self.tmp, "a")
        expected = "invalid api key; fix: CODEX_HOME=%s codex login" % path
        creds.annotate.assert_called_once_with("h1", "codex", "a", expected,
                                               expect_failures=2)
        self.assertEqual(rows[0]["reason"], expected)
        # once there, never twice
        failing["a"]["reason"] = expected
        rows, creds = self._check(failing, [same])
        creds.annotate.assert_not_called()

    def test_r2_clearing_write_is_conditional(self):
        # codex r2 (c): a turn records a failure between the read and the
        # write; the storage skips the conditional write, the row stays failing
        fresh = sentinel.probe_codex(self._codex({"tokens": {"refresh_token": "y"}},
                                                 mtime=NOW - 60), NOW)
        cfg = SimpleNamespace(host="h1", accounts={}, credential_sentinel={})
        creds = mock.Mock()
        creds.health.return_value = {"a": self._failing("x", NOW - 600)}
        creds.report.return_value = {"skipped": True}
        with mock.patch.object(sentinel, "probe_all", return_value=[fresh]), \
                mock.patch("ameesh.storage.of",
                           return_value=SimpleNamespace(credentials=creds)):
            rows = sentinel.check(cfg, object(), now=NOW)
        self.assertEqual(creds.report.call_args[1]["expect_failures"], 2)
        self.assertEqual(creds.report.call_args[1]["fingerprint"], sentinel._digest("y"))
        self.assertEqual((rows[0]["state"], rows[0]["previous"]), ("failing", "failing"))
        self.assertIn("recorded during the probe", rows[0]["kept"])
        # a row absent at read time expects no failure at all
        creds.health.return_value = {}
        with mock.patch.object(sentinel, "probe_all", return_value=[fresh]), \
                mock.patch("ameesh.storage.of",
                           return_value=SimpleNamespace(credentials=creds)):
            sentinel.check(cfg, object(), now=NOW)
        self.assertEqual(creds.report.call_args[1]["expect_failures"], 0)

    def test_r2_fingerprints_never_carry_the_secret(self):
        claude = accounts.Profile("claude", "c", path=_claude_dir(
            self.tmp, "c", refreshToken="sk-ant-ort01-secret"))
        codex = self._codex({"tokens": {"refresh_token": "rt-secret"}}, name="d")
        key = os.path.join(self.tmp, "k.key")
        with open(key, "w", encoding="utf-8") as fh:
            fh.write("sk-key")
        keyed = accounts.Profile("claude", "k", kind="api_key_env", key_file=key)
        env = {"DEEPSEEK_API_KEY_A": "dk"}
        by_env = accounts.Profile("deepseek", "e", kind="api_key_env",
                                  key_env="DEEPSEEK_API_KEY_A")
        found = [sentinel.fingerprint("claude", claude),
                 sentinel.fingerprint("codex", codex),
                 sentinel.fingerprint("claude", keyed),
                 sentinel.fingerprint("deepseek", by_env, env)]
        self.assertEqual(found, [sentinel._digest("sk-ant-ort01-secret"),
                                 sentinel._digest("rt-secret"), sentinel._digest("sk-key"),
                                 sentinel._digest("dk")])
        for value in found:
            self.assertRegex(value, r"^[0-9a-f]{24}$")
        # unknown harness, missing file: None
        self.assertIsNone(sentinel.fingerprint("deepseek", accounts.Profile(
            "deepseek", "x", path=self.tmp)))
        self.assertIsNone(sentinel.fingerprint("claude", accounts.Profile(
            "claude", "y", path=os.path.join(self.tmp, "absent"))))

    def test_r3_api_key_fingerprint_ignores_the_whitespace_the_runner_strips(self):
        # revoked-key failed, then rewritten as revoked-key\n: the runner still
        # passes revoked-key, the failure must stay
        key = os.path.join(self.tmp, "k.key")
        keyed = accounts.Profile("deepseek", "k", kind="api_key_env", key_file=key)
        by_env = accounts.Profile("deepseek", "e", kind="api_key_env",
                                  key_env="DEEPSEEK_API_KEY_A")
        found = set()
        for text in ("revoked-key", "revoked-key\n", "  revoked-key \r\n"):
            with open(key, "w", encoding="utf-8") as fh:
                fh.write(text)
            found.add(sentinel.fingerprint("deepseek", keyed))
            found.add(sentinel.fingerprint("deepseek", by_env, {"DEEPSEEK_API_KEY_A": text}))
        self.assertEqual(found, {sentinel._digest("revoked-key")})
        with open(key, "w", encoding="utf-8") as fh:
            fh.write("\n")
        self.assertIsNone(sentinel.fingerprint("deepseek", keyed))

    def test_p2_named_claude_probe_sheds_inherited_auth_variables(self):
        path = _claude_dir(self.tmp, "acct", refreshToken="x")
        profile = accounts.Profile("claude", "acct", path=path)
        declared = {"claude": [profile]}
        calls = []

        def run(argv, env=None, whole=False):
            calls.append(env)
            return 0, json.dumps({"loggedIn": True})
        environ = {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-inherited",
                   "CLAUDE_CONFIG_DIR": "/elsewhere"}
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/claude"):
            sentinel.probe_claude(profile, NOW, environ, run, declared)
        self.assertNotIn("ANTHROPIC_API_KEY", calls[0])
        self.assertEqual(calls[0]["CLAUDE_CONFIG_DIR"], path)
        # the default credential (no accounts declared) keeps them, as the runner does
        default = accounts.Profile("claude", "", path=path)
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/claude"):
            sentinel.probe_claude(default, NOW, environ, run, {})
        self.assertEqual(calls[1]["ANTHROPIC_API_KEY"], "sk-inherited")

    def test_p2_expiring_healthy_credential_keeps_the_fix_command(self):
        profile = accounts.Profile("claude", "e", path=_claude_dir(
            self.tmp, "e", refreshToken="x", refreshTokenExpiresAt=(NOW + 3600) * 1000))
        run = mock.Mock(return_value=(0, json.dumps({"loggedIn": True})))
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/claude"):
            v = sentinel.probe_claude(profile, NOW, run=run)
        self.assertEqual(v.state, "healthy")
        self.assertIn("fix: CLAUDE_CONFIG_DIR=%s claude auth login" % profile.path, v.text())
        creds = mock.Mock()
        creds.listing.return_value = [{
            "host": "h1", "harness": "claude", "account": "e", "state": "healthy",
            "reason": v.text(), "source": "sentinel", "checked_ts": NOW,
            "failing_since_ts": None, "expires_ts": v.expires_at}]
        with mock.patch.object(exploitation.storage, "of",
                               return_value=SimpleNamespace(credentials=creds)):
            [alert] = exploitation._credentials(object(), NOW)
        self.assertEqual(alert["type"], "credential_expiring")
        self.assertIn("claude auth login", alert["detail"])

    def test_p2_fix_commands_quote_paths(self):
        home = os.path.join(self.tmp, "account a")
        os.makedirs(home)
        claude = accounts.Profile("claude", "q", path=home)
        run = mock.Mock(return_value=(0, json.dumps({"loggedIn": False})))
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/claude"):
            v = sentinel.probe_claude(claude, NOW, run=run)
        self.assertIn("CLAUDE_CONFIG_DIR='%s' claude auth login" % home, v.text())
        codex = sentinel.probe_codex(accounts.Profile("codex", "q", path=home), NOW)
        self.assertIn("CODEX_HOME='%s' codex login" % home, codex.text())

    def test_p2_non_object_auth_json_is_unknown_and_later_probes_run(self):
        profile = self._codex([1])
        v = sentinel.probe_codex(profile, NOW)
        self.assertEqual(v.state, "unknown")
        cfg = SimpleNamespace(host="h1", credential_sentinel={"probes": ["codex", "gh"]},
                              accounts={"codex": [{"name": "a", "path": profile.path}]})
        run = mock.Mock(return_value=(0, ""))
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/x"):
            verdicts = sentinel.probe_all(cfg, NOW, run, environ={})
        self.assertEqual([(x.harness, x.state) for x in verdicts],
                         [("codex", "unknown"), ("gh", "healthy")])

    def test_p3_configured_warn_hours_is_used(self):
        cfg = SimpleNamespace(credential_sentinel={"warn_hours": 2})
        self.assertEqual(exploitation._credential_warn_s(cfg), 7200)
        creds = mock.Mock()
        creds.listing.return_value = [{
            "host": "h1", "harness": "gcloud", "account": "", "state": "healthy",
            "reason": "", "source": "sentinel", "checked_ts": NOW,
            "failing_since_ts": None, "expires_ts": NOW + 12 * 3600}]
        with mock.patch.object(exploitation.storage, "of",
                               return_value=SimpleNamespace(credentials=creds)):
            self.assertEqual(exploitation._credentials(object(), NOW,
                                                       exploitation._credential_warn_s(cfg)),
                             [])


class ReviewR5RacesTest(unittest.TestCase):
    """#210 review r5, P2 1 and 2 (L226c): a credential replaced during a
    probe, a failure recorded during the probes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l226c-")

    def _write(self, path: str, content: str):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_p2_1_claude_store_replaced_during_the_probe_is_unknown(self):
        # A probed, the owner installs B during `claude auth status`, A
        # answers not logged in: neither A nor B is recorded as failing
        home = _claude_dir(self.tmp, "a", refreshToken="A")
        profile = accounts.Profile("claude", "a", path=home)
        store = os.path.join(home, ".credentials.json")

        def run(argv, env=None, whole=False):
            if replace:
                self._write(store, json.dumps({"claudeAiOauth": {"refreshToken": "B"}}))
            return 1, json.dumps({"loggedIn": False})
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/claude"):
            replace = True
            v = sentinel.probe_claude(profile, NOW, run=run)
            self.assertEqual((v.state, v.reason, v.fingerprint),
                             ("unknown", sentinel.REPLACED, None))
            # unchanged during the probe: the failure carries B, read before
            replace = False
            v = sentinel.probe_claude(profile, NOW, run=run)
            self.assertEqual((v.state, v.fingerprint), ("failing", sentinel._digest("B")))

    def test_p2_1_cli_and_gcloud_store_replaced_during_the_probe(self):
        store = os.path.join(self.tmp, "hosts.yml")
        self._write(store, "A")

        def run(argv, output=False):
            self._write(store, "B")
            return 1, "HTTP 401: Bad credentials"
        with mock.patch.object(sentinel.shutil, "which", return_value="/bin/gh"):
            v = sentinel.probe_cli("gh", ["gh", "auth", "status"], "fix", run, store)
            self.assertEqual((v.state, v.reason), ("unknown", sentinel.REPLACED))
            # GH_TOKEN overrides the store: its replacement does not matter
            v = sentinel.probe_cli("gh", ["gh", "auth", "status"], "fix", run, store,
                                   override=sentinel._digest("T"))
            self.assertEqual((v.state, v.fingerprint), ("failing", sentinel._digest("T")))
            # #313 r1: through probe_all, no override — the store is read
            # once, before the probe: B replacing A during it is unknown,
            # never B's refusal recorded against A
            self._write(store, "A")
            cfg = SimpleNamespace(host="h1", accounts={},
                                  credential_sentinel={"probes": ["gh"]})
            found = sentinel.probe_all(cfg, NOW, run, environ={"GH_CONFIG_DIR": self.tmp})
            self.assertEqual((found[0].state, found[0].reason), ("unknown", sentinel.REPLACED))
            self._write(store, "A")
            found = sentinel.probe_all(cfg, NOW, run, environ={"GH_CONFIG_DIR": self.tmp,
                                                               "GH_TOKEN": "T"})
            self.assertEqual((found[0].state, found[0].fingerprint),
                             ("failing", sentinel._digest("T")))
            # the login lands between two reads before the probe (A, then B):
            # one read only, so the check after the probe sees the change
            reads = iter(["A", "B", "B", "B"])
            with mock.patch.object(sentinel, "file_fingerprint",
                                   side_effect=lambda path: next(reads)):
                found = sentinel.probe_all(cfg, NOW, mock.Mock(return_value=(1, "HTTP 401")),
                                           environ={"GH_CONFIG_DIR": self.tmp})
            self.assertEqual((found[0].state, found[0].reason), ("unknown", sentinel.REPLACED))
            # gcloud: credentials.db replaced during print-access-token
            db = os.path.join(self.tmp, "credentials.db")
            self._write(db, "A")

            def gcloud(argv, output=False):
                self._write(db, "B")
                return 0, "token"
            v = sentinel.probe_gcloud({"gcloud_session_hours": 16}, NOW, gcloud,
                                      environ={"CLOUDSDK_CONFIG": self.tmp})
            self.assertEqual((v.state, v.reason), ("unknown", sentinel.REPLACED))

    def test_p2_1_codex_fingerprint_is_the_file_read(self):
        home = os.path.join(self.tmp, "c")
        os.makedirs(home)
        self._write(os.path.join(home, "auth.json"),
                    json.dumps({"tokens": {"refresh_token": "A"}}))
        v = sentinel.probe_codex(accounts.Profile("codex", "c", path=home), NOW)
        self.assertEqual(v.fingerprint, sentinel._digest("A"))
        self.assertEqual(v.fingerprint, sentinel.fingerprint(
            "codex", accounts.Profile("codex", "c", path=home)))

    def test_p2_2_health_is_read_before_the_probes(self):
        # B probes healthy; meanwhile the owner installs C and C's turn fails:
        # the stale healthy B must not clear C's failure
        before = {"a": {"state": "failing", "failures": 1,
                        "failing_fingerprint": sentinel._digest("A")}}
        after = {"a": {"state": "failing", "failures": 2,
                       "failing_fingerprint": sentinel._digest("C")}}
        creds = mock.Mock()
        creds.health.return_value = before

        def probes(*args):
            creds.health.return_value = after      # C's turn fails during the probes
            return [sentinel.Verdict("codex", "a", "healthy", "ok",
                                     fingerprint=sentinel._digest("B"))]
        cfg = SimpleNamespace(host="h1", accounts={},
                              credential_sentinel={"probes": ["codex", "gh"]})
        creds.report.return_value = {"skipped": True}
        with mock.patch.object(sentinel, "probe_all", side_effect=probes), \
                mock.patch("ameesh.storage.of", return_value=SimpleNamespace(credentials=creds)):
            rows = sentinel.check(cfg, object(), now=NOW)
        # the write expects the count read before the probes: the storage skips it
        self.assertEqual(creds.report.call_args[1]["expect_failures"], 1)
        self.assertEqual(rows[0]["state"], "failing")
        self.assertEqual([c[0][1] for c in creds.health.call_args_list], ["codex", "gh"])
        # dry run: no read at all
        creds.reset_mock()
        with mock.patch.object(sentinel, "probe_all", side_effect=probes):
            sentinel.check(cfg, None, now=NOW, dry_run=True)
        creds.health.assert_not_called()


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
