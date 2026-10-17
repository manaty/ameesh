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
from ameesh import sentinel
from ameesh import storage


class ProbeHealthTest(unittest.TestCase):
    def test_gh_sonde_sans_jeton_ambiant_dans_le_foyer_demande(self):
        environ = {"GH_CONFIG_DIR": "/another", "GH_TOKEN": "canary",
                   "GITHUB_TOKEN": "canary", "GH_HOST": "evil.example"}
        profile = accounts_mod.Profile(harness="gh", name="", path=self.home, declared=True)
        seen = {}
        def run(_argv, **kwargs):
            seen["argv"] = list(_argv)
            seen.update(kwargs.get("env") or {})
            return 1, "not logged in"
        with mock.patch("ameesh.sentinel.shutil.which", return_value="gh"):
            verdict = login_cli.probe_health(self.cfg, profile, "gh", environ=environ, run=run)
        self.assertNotIn("GH_TOKEN", seen)
        self.assertNotIn("GITHUB_TOKEN", seen)
        self.assertNotIn("GH_HOST", seen)
        # N5 : la sonde vise github.com comme l'adaptateur de connexion
        self.assertEqual(seen.get("argv"),
                         ["gh", "auth", "status", "--hostname", "github.com"])
        self.assertEqual(seen.get("GH_CONFIG_DIR"), self.home)
        self.assertEqual(verdict.state, "failing")

    def test_gh_empreinte_suit_le_jeton_effectif_pas_le_fichier(self):
        """B2 : un commentaire dans hosts.yml ne change pas la credential."""
        profile = accounts_mod.Profile(harness="gh", name="", path=self.home,
                                       declared=True)
        token = {"value": "gho_first"}
        def run(argv, **kwargs):
            if list(argv)[2] == "token":
                return 0, token["value"] + "\n"
            return 0, "Logged in to github.com"
        with mock.patch("ameesh.sentinel._run", side_effect=run):
            first = login_cli.credential_fingerprint("gh", profile, environ={})
            with open(os.path.join(self.home, "hosts.yml"), "w", encoding="utf-8") as fh:
                fh.write("# unrelated comment\ngithub.com:\n  oauth_token: gho_first\n")
            same = login_cli.credential_fingerprint("gh", profile, environ={})
            token["value"] = "gho_second"
            second = login_cli.credential_fingerprint("gh", profile, environ={})
        self.assertEqual(first, sentinel._digest("gho_first"))
        self.assertEqual(same, first)        # le fichier a changé, pas la credential
        self.assertNotEqual(second, first)   # la credential a changé

    def test_gh_sans_jeton_effectif_le_verdict_ne_porte_aucune_empreinte(self):
        """B2 : sans jeton lisible (keyring verrouillé, aucun jeton), pas de
        repli sur le fichier ; la preuve de credential neuve est refusée."""
        profile = accounts_mod.Profile(harness="gh", name="", path=self.home,
                                       declared=True)
        def run(argv, **kwargs):
            if list(argv)[2] == "token":
                return 1, "no oauth token found for github.com"
            return 0, "Logged in to github.com"
        with mock.patch("ameesh.sentinel.shutil.which", return_value="gh"):
            verdict = login_cli.probe_health(self.cfg, profile, "gh",
                                             environ={}, run=run)
        self.assertEqual(verdict.state, "healthy")
        self.assertIsNone(verdict.fingerprint)

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
    def test_panne_pendant_la_sonde_ne_devient_pas_la_reference_du_cas(self):
        def probe(*_args, **_kwargs):
            self.credentials.failures += 1
            return SimpleNamespace(state="healthy", text=lambda: "healthy",
                                   expires_at=None, fingerprint="new")
        with mock.patch.object(login_cli, "probe_health", side_effect=probe):
            login_cli.health_report(self.store, self.cfg, None, self.request)
        self.assertEqual(self.credentials.calls[0][-1]["expect_failures"], 3)

    def test_une_ecriture_refusee_ne_rend_pas_healthy(self):
        self.write_auth()
        profile = accounts_mod.parse(self.cfg.accounts)["codex"][0]
        with mock.patch.object(self.credentials, "report", return_value={"skipped": True}):
            self.assertEqual(login_cli.health_report(
                self.store, self.cfg, profile, self.request, environ={}), "failing")

    def test_ancienne_credential_en_echec_reste_bloquee(self):
        verdict = sentinel.Verdict("codex", "default", "healthy", "old", fingerprint="same")
        with mock.patch.object(login_cli, "probe_health", return_value=verdict), \
                mock.patch.object(self.credentials, "health", return_value={"default": {
                    "state": "failing", "failing_fingerprint": "same", "failures": 3}}):
            self.assertEqual(login_cli.health_report(
                self.store, self.cfg, None, self.request), "failing")
        self.assertEqual(self.credentials.calls, [])

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
        # valeur admise par credential_health_source_check : le témoin E2E #366 a
        # montré que « login » viole la contrainte, et `ameesh accounts check`
        # écrit déjà « sentinel » avec exactement les mêmes sondes
        self.assertEqual(kwargs["source"], "sentinel")
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

    def test_empreinte_avant_apres_suit_la_credential(self):
        self.write_auth("old")
        profile = accounts_mod.parse(self.cfg.accounts)["codex"][0]
        first = login_cli.credential_fingerprint("codex", profile, environ={})
        self.assertIsNotNone(first)
        self.write_auth("new")
        second = login_cli.credential_fingerprint("codex", profile, environ={})
        self.assertNotEqual(first, second)

    def test_meme_credential_avant_apres_ne_prouve_pas_une_connexion_neuve(self):
        """N2 : un verdict sain sur la MÊME empreinte n'est pas une preuve."""
        verdict = sentinel.Verdict("codex", "default", "healthy", "present",
                                   fingerprint="same")
        with mock.patch.object(login_cli, "probe_health", return_value=verdict):
            state = login_cli.health_report(self.store, self.cfg, None, self.request,
                                            credential_before="same")
        self.assertEqual(state, "failing")
        self.assertEqual(self.credentials.calls, [])

    def test_credential_differente_prouve_la_connexion(self):
        verdict = sentinel.Verdict("codex", "default", "healthy", "present",
                                   fingerprint="new")
        with mock.patch.object(login_cli, "probe_health", return_value=verdict):
            state = login_cli.health_report(self.store, self.cfg, None, self.request,
                                            credential_before="old")
        self.assertEqual(state, "healthy")
        self.assertEqual(len(self.credentials.calls), 1)

    def test_aucune_credential_avant_est_une_preuve(self):
        verdict = sentinel.Verdict("codex", "default", "healthy", "present",
                                   fingerprint="new")
        with mock.patch.object(login_cli, "probe_health", return_value=verdict):
            state = login_cli.health_report(self.store, self.cfg, None, self.request,
                                            credential_before=None)
        self.assertEqual(state, "healthy")
        self.assertEqual(len(self.credentials.calls), 1)

    def test_absence_d_empreinte_apres_ne_prouve_rien(self):
        verdict = sentinel.Verdict("codex", "default", "healthy", "present",
                                   fingerprint=None)
        with mock.patch.object(login_cli, "probe_health", return_value=verdict):
            state = login_cli.health_report(self.store, self.cfg, None, self.request,
                                            credential_before=None)
        self.assertEqual(state, "failing")
        self.assertEqual(self.credentials.calls, [])

    def test_gh_commentaire_du_fichier_ne_prouve_pas_une_credential_neuve(self):
        """B2 : hosts.yml change (commentaire) sans que le jeton change → la
        sonde porte la même empreinte et la connexion n'est pas « neuve »."""
        profile = accounts_mod.Profile(harness="gh", name="", path=self.home,
                                       declared=True)
        def run(argv, **kwargs):
            if list(argv)[2] == "token":
                return 0, "gho_same\n"
            return 0, "Logged in to github.com"
        with open(os.path.join(self.home, "hosts.yml"), "w", encoding="utf-8") as fh:
            fh.write("# unrelated comment\ngithub.com: {}\n")
        request = SimpleNamespace(harness="gh", account="")
        with mock.patch("ameesh.sentinel.shutil.which", return_value="gh"):
            state = login_cli.health_report(
                self.store, self.cfg, profile, request, environ={}, run=run,
                credential_before=sentinel._digest("gho_same"))
        self.assertEqual(state, "failing")
        self.assertEqual(self.credentials.calls, [])

    def test_gh_jeton_remplace_pendant_la_sonde_ne_prouve_pas_la_connexion(self):
        """La santé de l'ancien jeton ne peut pas valider le nouveau."""
        profile = accounts_mod.Profile(harness="gh", name="", path=self.home,
                                       declared=True)
        token = "gho_new_unvalidated"
        def run(argv, **kwargs):
            nonlocal token
            if list(argv)[2] == "token":
                return 0, token + "\n"
            token = "gho_old_healthy"
            return 0, "Logged in to github.com"
        request = SimpleNamespace(harness="gh", account="")
        with mock.patch("ameesh.sentinel.shutil.which", return_value="gh"):
            state = login_cli.health_report(
                self.store, self.cfg, profile, request, environ={}, run=run,
                credential_before=sentinel._digest("gho_old_healthy"))
        self.assertEqual(state, "unknown")
        (_, _, _, written, kwargs), = self.credentials.calls
        self.assertEqual(written, "unknown")
        self.assertIsNone(kwargs["fingerprint"])
        self.assertEqual(kwargs["reason"], sentinel.REPLACED)


if __name__ == "__main__":
    unittest.main()
