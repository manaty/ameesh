# SPDX-License-Identifier: AGPL-3.0-only
"""L232d (#366) : la santé après connexion (`probe_health`, `health_report`).

Une connexion « terminée » ne prouve pas le grant : la sonde `sentinel` relit
le foyer du compte et c'est son verdict qui est écrit dans `credential_health`,
la table que lit déjà `host_preflight` (L226b). Ici, aucun CLI réel, aucun
réseau, aucune base : la sonde reçoit un `run` factice et le magasin est un
double en mémoire.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import accounts as accounts_mod
from ameesh import login_cli
from ameesh import storage


class ProbeHealthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="l232d-health-")
        self.addCleanup(self.temp.cleanup)
        self.home = os.path.join(self.temp.name, "home")
        os.makedirs(self.home, mode=0o700)
        self.cfg = SimpleNamespace(host="host-1", accounts={
            "claude": [{"name": "default", "path": self.home}],
            "codex": [{"name": "default", "path": self.home}],
        })
        self.declared = accounts_mod.parse(self.cfg.accounts)

    def claude_profile(self):
        return self.declared["claude"][0]

    def codex_profile(self):
        return self.declared["codex"][0]

    def claude_binary(self, environ):
        bindir = os.path.join(self.temp.name, "bin")
        os.makedirs(bindir, mode=0o700)
        path = os.path.join(bindir, "claude")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
        environ["PATH"] = bindir

    def test_claude_sain_relit_le_foyer_du_compte(self):
        now = 1_700_000_000.0
        with open(os.path.join(self.home, ".credentials.json"), "w", encoding="utf-8") as fh:
            json.dump({"claudeAiOauth": {
                "refreshTokenExpiresAt": int((now + 3600) * 1000)}}, fh)
        environ = {}
        self.claude_binary(environ)
        seen = {}

        def run(argv, env=None, whole=False):
            seen["argv"] = list(argv)
            seen["env"] = dict(env or {})
            return 0, json.dumps({"loggedIn": True, "authMethod": "claudeai"})

        verdict = login_cli.probe_health(self.cfg, self.claude_profile(), "claude",
                                         environ=environ, run=run, now=now,
                                         declared=self.declared)
        self.assertEqual(verdict.state, "healthy")
        self.assertEqual(seen["argv"], ["claude", "auth", "status"])
        # la sonde vise le foyer déclaré, jamais la variable ambiante
        self.assertEqual(seen["env"]["CLAUDE_CONFIG_DIR"], self.home)

    def test_codex_sain_puis_sans_jeton(self):
        path = os.path.join(self.home, "auth.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tokens": {"refresh_token": "r"}}, fh)
        verdict = login_cli.probe_health(self.cfg, self.codex_profile(), "codex",
                                         environ={}, now=1_700_000_000.0)
        self.assertEqual(verdict.state, "healthy")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tokens": {}}, fh)
        verdict = login_cli.probe_health(self.cfg, self.codex_profile(), "codex",
                                         environ={}, now=1_700_000_000.0)
        self.assertEqual(verdict.state, "failing")
        self.assertIn("no refresh token", verdict.reason)

    def test_harnais_sans_sonde_rend_None(self):
        profile = self.codex_profile()
        self.assertIsNone(login_cli.probe_health(self.cfg, profile, "dsh",
                                                 environ={}, now=1.0))


class _FakeCredentials:
    def __init__(self, failures=0):
        self.calls = []
        self.failures = failures

    def health(self, host, harness):
        return {"default": {"failures": self.failures}}

    def report(self, host, harness, account, state, **kwargs):
        self.calls.append((host, harness, account, state, kwargs))


class HealthReportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="l232d-health-report-")
        self.addCleanup(self.temp.cleanup)
        self.home = os.path.join(self.temp.name, "home")
        os.makedirs(self.home, mode=0o700)
        self.cfg = SimpleNamespace(host="host-1", accounts={
            "codex": [{"name": "default", "path": self.home}]})
        self.credentials = _FakeCredentials(failures=3)
        self.store = SimpleNamespace(credentials=self.credentials)
        patcher = mock.patch.object(storage, "of", lambda db: self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.request = SimpleNamespace(harness="codex", account="default")

    def write_auth(self, token="r"):
        with open(os.path.join(self.home, "auth.json"), "w", encoding="utf-8") as fh:
            json.dump({"tokens": {"refresh_token": token}}, fh)

    def test_verdict_sain_ecrit_la_table_avec_la_source_login(self):
        self.write_auth()
        profile = accounts_mod.parse(self.cfg.accounts)["codex"][0]
        state = login_cli.health_report(self.store, self.cfg, profile, self.request,
                                        environ={}, now=1_700_000_000.0)
        self.assertEqual(state, "healthy")
        (host, harness, account, written, kwargs), = self.credentials.calls
        self.assertEqual((host, harness, account, written),
                         ("host-1", "codex", "default", "healthy"))
        self.assertEqual(kwargs["source"], "login")     # jamais « sentinel »
        self.assertEqual(kwargs["expect_failures"], 3)  # l'historique est conservé
        self.assertIn("refresh token present", kwargs["reason"])
        self.assertIsNotNone(kwargs["fingerprint"])

    def test_harnais_sans_sonde_n_ecrit_rien(self):
        profile = accounts_mod.parse(self.cfg.accounts)["codex"][0]
        state = login_cli.health_report(self.store, self.cfg, profile,
                                        SimpleNamespace(harness="dsh", account="default"),
                                        environ={}, now=1.0)
        self.assertEqual(state, "")
        self.assertEqual(self.credentials.calls, [])


if __name__ == "__main__":
    unittest.main()
