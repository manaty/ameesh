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
    def test_harnais_inconnu_refuse(self):
        cfg = SimpleNamespace(accounts={})
        self.assertEqual(login_cli.cmd_accounts_login(cfg, args(harness="dsh")), 1)

    def test_aucun_compte_declare_refuse(self):
        cfg = SimpleNamespace(accounts={})
        self.assertEqual(login_cli.cmd_accounts_login(cfg, args(harness="claude")), 1)


if __name__ == "__main__":
    unittest.main()
