# SPDX-License-Identifier: AGPL-3.0-only
"""L232d (#366) : la surface `ameesh accounts login`, sans service ni réseau.

Les helpers sont testés à leurs frontières : identité, résolution du profil
déclaré, foyer privé, épinglage de version, refus de la commande. Le chemin
complet (défi publié, vue privée, reçu, santé) a son témoin de bout en bout
ailleurs ; ici, aucun CLI n'est lancé.
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import accounts as accounts_mod
from ameesh import login
from ameesh import login_cli


def args(**overrides):
    values = {"harness": "gh", "account": None, "approver": "human:alice", "scope": "",
              "wait": 5.0, "json": False, "by": None}
    values.update(overrides)
    return SimpleNamespace(**values)


class ActorTest(unittest.TestCase):
    def test_agent_lie_puis_utilisateur(self):
        self.assertEqual(login_cli.actor({"AGENT_MAIL_NAME": "impl-04"}), "agent:impl-04")
        self.assertEqual(login_cli.actor({"USER": "alice"}), "human:alice")
        self.assertEqual(login_cli.actor({"AMEESH_ACTOR": "bob", "USER": "alice"}), "human:bob")

    def test_adaptateurs_connus(self):
        self.assertEqual(login_cli.adapter_names(), ("claude", "codex", "gh"))


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="l232d-login-cli-")
        self.addCleanup(self.temp.cleanup)
        self.home = os.path.join(self.temp.name, "home")
        os.makedirs(self.home, mode=0o700)
        self.cfg = SimpleNamespace(accounts={"claude": [
            {"name": "default", "path": self.home},
            {"name": "second", "path": os.path.join(self.temp.name, "second")},
        ]})
        os.makedirs(os.path.join(self.temp.name, "second"), mode=0o700)

    def test_profil_par_nom_puis_primaire(self):
        named = login_cli.resolve_profile(self.cfg, "claude", "second")
        self.assertEqual(named.name, "second")
        primary = login_cli.resolve_profile(self.cfg, "claude", None)
        self.assertIn(primary.name, ("default", "second"))

    def test_profil_inconnu_ou_absent(self):
        with self.assertRaises(login.LoginError) as caught:
            login_cli.resolve_profile(self.cfg, "claude", "nope")
        self.assertEqual(caught.exception.category, "failed")
        empty = SimpleNamespace(accounts={})
        with self.assertRaises(login.LoginError) as caught:
            login_cli.resolve_profile(empty, "claude", None)
        self.assertEqual(caught.exception.category, "failed")

    def test_gh_est_une_connexion_unique_sans_compte_declare(self):
        empty = SimpleNamespace(accounts={})
        home = os.path.join(self.temp.name, "gh")
        profile = login_cli.resolve_profile(empty, "gh", None,
                                            environ={"GH_CONFIG_DIR": home})
        self.assertEqual(profile.name, "")
        self.assertEqual(profile.home({}), home)
        # le dossier peut manquer avant la connexion : gh le crée
        self.assertEqual(login_cli.home_problems(profile, {}), [])
        os.makedirs(home, mode=0o700)
        self.assertEqual(login_cli.home_problems(profile, {}), [])
        os.chmod(home, 0o770)
        self.assertIn("writable by others", " ".join(login_cli.home_problems(profile, {})))
        with self.assertRaises(login.LoginError):
            login_cli.resolve_profile(empty, "gh", "other")
        self.assertEqual(
            login_cli.resolve_profile(empty, "gh", None, environ={}).home({}),
            os.path.expanduser(login_cli.GH_HOME))

    def test_foyer_prive_ou_refuse(self):
        profile = accounts_mod.parse(self.cfg.accounts)["claude"][0]
        self.assertEqual(login_cli.home_problems(profile), [])
        os.chmod(self.home, 0o755)
        self.assertIn("not private", " ".join(login_cli.home_problems(profile)))
        os.chmod(self.home, 0o700)
        missing = accounts_mod.Profile(harness="claude", name="gone",
                                       path=os.path.join(self.temp.name, "gone"),
                                       declared=True)
        self.assertIn("missing directory", " ".join(login_cli.home_problems(missing)))
        default = accounts_mod.Profile(harness="claude", name="def", declared=True)
        self.assertEqual(login_cli.home_problems(default), [])

    def test_version_epinglee_ou_refusee(self):
        good = login_cli.pinned_version(
            "gh", probe=lambda harness: {"version": "2.102.0", "error": None})
        self.assertEqual(good["version"], "2.102.0")
        for result in ({"version": "2.101.0", "error": None},
                       {"version": None, "error": "not found: gh"},
                       {"version": "2.102.0-rc.1", "error": None}):
            with self.assertRaises(login.LoginError) as caught:
                login_cli.pinned_version("gh", probe=lambda harness, r=result: r)
            self.assertEqual(caught.exception.category, "unsupported_version")


class CommandTest(unittest.TestCase):
    def test_version_sonde_le_meme_executable_que_la_connexion(self):
        environ = {"PATH": "/bin", "GH_TOKEN": "canary", "GH_HOST": "evil.example"}
        with mock.patch("subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout="gh version 2.102.0\n", stderr="")) as run:
            self.assertEqual(login_cli.pinned_version(
                "gh", environ=environ, home="/srv/gh")["version"], "2.102.0")
        self.assertEqual(run.call_args.args[0], ["gh", "--version"])
        # N6 : la sonde de version ne voit pas les clés héritées et vise le
        # foyer du compte, comme la connexion
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GH_CONFIG_DIR"], "/srv/gh")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GH_HOST", env)

    def test_profil_cle_api_refuse_avant_la_connexion_oauth(self):
        cfg = SimpleNamespace(accounts={"codex": [
            {"name": "key", "type": "api_key_env", "key_env": "OPENAI_API_KEY"}]})
        with self.assertRaises(login.LoginError):
            login_cli.resolve_profile(cfg, "codex", "key", environ={})

    def test_harnais_inconnu_refuse(self):
        cfg = SimpleNamespace(accounts={})
        self.assertEqual(login_cli.cmd_accounts_login(cfg, args(harness="dsh")), 1)

    def test_aucun_compte_declare_refuse(self):
        cfg = SimpleNamespace(accounts={})
        self.assertEqual(login_cli.cmd_accounts_login(cfg, args(harness="claude")), 1)


class CommandCleanupTest(unittest.TestCase):
    def test_arret_sur_absence_de_publication_erreur_ou_interruption(self):
        for event in ("unpublished", "fetch_error", "publish_interrupt", "bad_receipt"):
            with self.subTest(event=event):
                cfg = SimpleNamespace(host="h")
                sup = login.Supervisor(grace=0)
                request = sup.registry.open(host="h", harness="codex", account="default")
                if event != "unpublished":
                    request.state = login.VIEWING
                    request.login_id = "lgn_" + "a" * 26
                proc = mock.Mock(pid=424242)
                proc.returncode = None
                sup._procs[request.id] = proc
                sup.begin = mock.Mock(return_value=request)
                sup.wait = mock.Mock(side_effect=KeyboardInterrupt if event == "publish_interrupt" else None)
                error = login.LoginError("failed", "synthetic failure")
                with mock.patch.object(login_cli, "resolve_profile", return_value=mock.Mock(name="default")), \
                        mock.patch.object(login_cli, "home_problems", return_value=[]), \
                        mock.patch.object(login_cli, "pinned_version", return_value={"version": "0.159.3"}), \
                        mock.patch.object(login_cli, "credential_fingerprint", return_value=None), \
                        mock.patch("ameesh.db.open_db", return_value=mock.Mock()), \
                        mock.patch.object(login, "Supervisor", return_value=sup), \
                        mock.patch("ameesh.actions.policy_from_env", return_value=None), \
                        mock.patch("ameesh.approve_client.fetch_login_receipt", side_effect=error
                                   if event == "fetch_error" else None,
                                   return_value=("signed", b"{}")), \
                        mock.patch.object(login, "receipt_decision", side_effect=error), \
                        mock.patch.object(login.os, "killpg") as kill:
                    def stopped(*_args):
                        proc.returncode = -15
                    kill.side_effect = stopped
                    try:
                        login_cli._run(cfg, args(harness="codex", json=True))
                    except (login.LoginError, KeyboardInterrupt):
                        pass
                    self.assertEqual(request.state, login.CANCELLED)
                    self.assertEqual(proc.returncode, -15)


if __name__ == "__main__":
    unittest.main()
