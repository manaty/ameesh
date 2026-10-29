# SPDX-License-Identifier: AGPL-3.0-only
"""Host-local token import, environment isolation, expiry and failure retention."""
import contextlib
import io
import os
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import accounts, exploitation, login_cli, sentinel, setup_token

TOKEN = "sk-ant-oat01-test-only-no-provider-access"
NOW = 1800000000


class SetupTokenTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.home = self.root.name
        self.profile = accounts.Profile("claude", "primary", path=self.home, declared=True)

    def test_import_launch_and_no_secret_in_output(self):
        setup_token.install(self.profile, TOKEN, expires_at=NOW + 40 * 86400)
        target = setup_token.path(self.profile)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        env = {"CLAUDE_CONFIG_DIR": "ambient", "ANTHROPIC_API_KEY": "billed",
               "CLAUDE_CODE_OAUTH_TOKEN": "ambient", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "other",
               "ANTHROPIC_AUTH_TOKEN": "ambient", "CODEX_API_KEY": "ambient"}
        accounts.launch_env(env, {"claude": [self.profile]}, self.profile, {})
        self.assertEqual(env[setup_token.VARIABLE], TOKEN)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], self.home)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", env)
        self.assertEqual(accounts.check(self.profile, {}), [])
        verdict = sentinel.probe_claude(self.profile, NOW, environ={})
        self.assertEqual(verdict.state, "healthy")
        self.assertEqual(verdict.expires_at, NOW + 40 * 86400)
        self.assertNotIn(TOKEN, repr(verdict))
        self.assertEqual(verdict.fingerprint, sentinel.fingerprint("claude", self.profile, {}))

    def test_launch_fingerprint_survives_concurrent_token_import(self):
        setup_token.install(self.profile, TOKEN)
        env = {}
        accounts.launch_env(env, {"claude": [self.profile]}, self.profile, {})
        replacement = TOKEN + "-replacement"
        setup_token.install(self.profile, replacement)
        self.assertEqual(sentinel.launch_fingerprint("claude", self.profile, env),
                         sentinel._digest(TOKEN))
        self.assertEqual(sentinel.fingerprint("claude", self.profile, {}),
                         sentinel._digest(replacement))
        os.unlink(setup_token.path(self.profile))
        self.assertEqual(sentinel.launch_fingerprint("claude", self.profile, env),
                         sentinel._digest(TOKEN))

    def test_empty_child_token_does_not_fall_back_to_disk(self):
        setup_token.install(self.profile, TOKEN)
        self.assertIsNone(sentinel.launch_fingerprint(
            "claude", self.profile, {setup_token.VARIABLE: ""}))

    def test_permission_symlink_and_malformed_store_refused_without_leak(self):
        setup_token.install(self.profile, TOKEN)
        target = setup_token.path(self.profile)
        os.chmod(target, 0o644)
        self.assertTrue(accounts.check(self.profile, {}))
        self.assertEqual(sentinel.probe_claude(self.profile, NOW, {}).state, "failing")
        os.unlink(target)
        os.symlink(os.path.join(self.home, "missing"), target)
        self.assertTrue(setup_token.present(self.profile))
        with self.assertRaises(setup_token.TokenError):
            setup_token.read(self.profile)
        os.unlink(target)
        with open(target, "w") as handle:
            handle.write(TOKEN)
        os.chmod(target, 0o600)
        with self.assertRaises(setup_token.TokenError) as error:
            setup_token.read(self.profile)
        self.assertNotIn(TOKEN, str(error.exception))

    def test_no_api_key_and_no_other_harness(self):
        for value in ("sk-ant-api03-usage-billed", TOKEN + "\nsecond-line"):
            with self.assertRaises(setup_token.TokenError):
                setup_token.install(self.profile, value)
        other = accounts.Profile("codex", "primary", path=self.home, declared=True)
        with self.assertRaises(setup_token.TokenError):
            setup_token.install(other, TOKEN)

    def test_store_refuses_git_worktree_and_permissive_home(self):
        import subprocess
        subprocess.run(["git", "init", "-q", self.home], check=True)
        with self.assertRaises(setup_token.TokenError):
            setup_token.install(self.profile, TOKEN)
        import shutil
        shutil.rmtree(os.path.join(self.home, ".git"))
        os.chmod(self.home, 0o755)
        with self.assertRaises(setup_token.TokenError):
            setup_token.install(self.profile, TOKEN)

    def test_other_account_and_harness_never_inherit_token(self):
        setup_token.install(self.profile, TOKEN)
        other_home = os.path.join(self.home, "other")
        os.mkdir(other_home, 0o700)
        with open(os.path.join(other_home, "auth.json"), "w") as handle:
            handle.write("{}")
        other = accounts.Profile("codex", "other", path=other_home, declared=True)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN,
               "ANTHROPIC_AUTH_TOKEN": "ambient", "CODEX_API_KEY": "ambient"}
        accounts.launch_env(env, {"claude": [self.profile], "codex": [other]}, other, {})
        self.assertNotIn(setup_token.VARIABLE, env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertEqual(env["CODEX_HOME"], other_home)

    def test_opaque_expiry_unknown_and_expired_failing(self):
        setup_token.install(self.profile, TOKEN)
        self.assertIsNone(sentinel.probe_claude(self.profile, NOW, {}).expires_at)
        setup_token.install(self.profile, TOKEN, expires_at=NOW - 1)
        self.assertEqual(sentinel.probe_claude(self.profile, NOW, {}).state, "failing")
        with mock.patch.object(accounts.time, "time", return_value=NOW):
            with self.assertRaises(accounts.AccountError):
                accounts.apply_env({}, self.profile, {})

    def test_turn_rejection_cannot_be_cleared_by_same_token_presence(self):
        setup_token.install(self.profile, TOKEN)
        verdict = sentinel.probe_claude(self.profile, NOW, {})
        current = {"state": "failing", "failing_fingerprint": verdict.fingerprint}
        self.assertTrue(sentinel.kept_failure(current, verdict))
        setup_token.install(self.profile, TOKEN + "-replacement")
        self.assertFalse(sentinel.kept_failure(current, sentinel.probe_claude(self.profile, NOW, {})))

    def test_cli_stdin_import_and_codex_refusal(self):
        cfg = SimpleNamespace(host="host-a", accounts={"claude": [{"name": "primary", "path": self.home}]})
        args = SimpleNamespace(harness="claude", account="primary", token_file="-", expires_at=NOW + 10)
        with mock.patch.object(login_cli.sys, "stdin", io.StringIO(TOKEN)), \
                mock.patch("ameesh.db.open_db") as database, \
                mock.patch.object(login_cli, "health_report", return_value="healthy"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(login_cli.import_setup_token(cfg, args), 0)
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertEqual(setup_token.read(self.profile)[0], TOKEN)
        database.return_value.close.assert_called_once()
        args.harness = "codex"
        with self.assertRaises(Exception):
            login_cli.import_setup_token(cfg, args)

    def test_notification_only_contains_private_approval_link(self):
        cfg = SimpleNamespace(notify={})
        req = SimpleNamespace(harness="codex", account="primary", host="host-a", link="https://approve.test/login/opaque")
        ncfg = mock.Mock(timeout=2)
        ncfg.channels_for.return_value = [mock.Mock()]
        with mock.patch("ameesh.notify.parse_config", return_value=ncfg), \
                mock.patch("ameesh.notify.Sender") as sender:
            login_cli.notify_login(cfg, "human:owner", req)
        message = sender.return_value.send.call_args.args[1]
        self.assertIn(req.link, message.body)
        self.assertNotIn("device", message.body)


class ExpiryAlertTest(unittest.TestCase):
    def test_rotating_oauth_at_25_days_does_not_alert(self):
        from ameesh import storage
        store = mock.Mock()
        rows = [dict(host="host-a", harness=harness, account="primary", state="healthy",
                     reason="OAuth credentials", expires_ts=NOW + 25 * 86400)
                for harness in ("claude", "codex")]
        store.credentials.listing.return_value = rows
        with mock.patch.object(storage, "of", return_value=store):
            self.assertEqual(exploitation._credentials(None, NOW), [])
            rows[0]["expires_ts"] = NOW + sentinel.DEFAULT_WARN_HOURS * 3600
            self.assertEqual(len(exploitation._credentials(None, NOW)), 1)

    def test_warn_at_30_days_and_immediate_failure(self):
        from ameesh import storage
        cfg = SimpleNamespace(credential_sentinel={})
        self.assertEqual(sentinel.ACCOUNT_WARN_HOURS, 30 * 24)
        rows = [dict(host="host-a", harness="claude", account="primary", state="healthy",
                     reason="setup-token installed", expires_ts=NOW + 30 * 86400)]
        store = mock.Mock()
        store.credentials.listing.return_value = rows
        with mock.patch.object(storage, "of", return_value=store):
            alerts = exploitation._credentials(None, NOW)
            self.assertEqual([a["type"] for a in alerts], ["credential_expiring"])
            rows[0]["expires_ts"] += 1
            self.assertEqual(exploitation._credentials(None, NOW), [])
            rows[0].update(state="failing", checked_ts=NOW)
            self.assertEqual(exploitation._credentials(None, NOW)[0]["type"], "credential_failing")


from .test_l30_accounts import _Base


class TokenRunnerTest(_Base):
    def test_runner_turn_uses_setup_token_without_config_dir(self):
        import subprocess
        profiles = self._claude_comptes()
        profile = accounts.parse({"claude": profiles})["claude"][0]
        setup_token.install(profile, TOKEN, expires_at=NOW + 40 * 86400)
        runner, worker = self._worker("setup-token-turn", "claude", {"claude": profiles})
        original = subprocess.Popen
        observed = []

        def spawn(argv, *args, **kwargs):
            if any("fakebin/claude" in str(item) for item in argv):
                env = kwargs.get("env") or {}
                observed.append((env.get(setup_token.VARIABLE) == TOKEN,
                                 "CLAUDE_CONFIG_DIR" in env,
                                 "ANTHROPIC_API_KEY" in env))
            return original(argv, *args, **kwargs)

        try:
            with mock.patch("subprocess.Popen", side_effect=spawn):
                result = self._tour(worker, ANTHROPIC_API_KEY="ambient-billed-key")
            self.assertIsNotNone(result)
            self.assertEqual(observed, [(True, False, False)])
        finally:
            worker.watchdog_stop.set()
