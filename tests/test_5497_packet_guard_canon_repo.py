# SPDX-License-Identifier: AGPL-3.0-only
"""#5497 : la garde du paquet (#2333) dépend du dépôt.

Le §11 bis vise `main` d'ameesh et `develop` de Nexlink. Le dépôt du canon
(lu dans la configuration, jamais en dur) est fusionné par le bureau (#2511) :
une PR `canon/**` vers son `main` est admise. Un lot vers `main` d'un autre
dépôt reste refusé, de même qu'un dépôt non déclaré ou illisible.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
import unittest

from ameesh import packet_guard

from .test_1003_merge_pr import _Base, _git
from .test_l160b_radar import git_env

HEAD = "a" * 40


def _clone_with_origin(root: str, name: str, url: str) -> str:
    path = os.path.join(root, name)
    os.makedirs(path)
    _git(path, "init", "-q")
    _git(path, "remote", "add", "origin", url)
    return path


def _cfg(path: str, *, untrusted: bool = False):
    return types.SimpleNamespace(canon=path, canon_ref="", canon_untrusted=untrusted)


class RepositoryGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-5497-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.canon = _clone_with_origin(self.tmp, "home", "git@github.com:manaty/home.git")
        self.cfg = _cfg(self.canon)

    def test_a_canon_branch_into_the_canon_main_is_admitted(self):
        for repo in ("manaty/home", "Manaty/Home",
                     _clone_with_origin(self.tmp, "co", "https://github.com/manaty/home")):
            with self.subTest(repo=repo):
                out = packet_guard.check_merge(None, 5447, "canon/5447-avis", "main", HEAD,
                                               repo=repo, cfg=self.cfg)
                self.assertTrue(out["allowed"])
                self.assertIn("#2511", out["why"])
                self.assertFalse(out["journaled"])

    def test_a_lot_branch_into_another_main_is_refused_as_before(self):
        for repo in ("manaty/ameesh", "manaty/nexlink", "",
                     _clone_with_origin(self.tmp, "am", "git@github.com:manaty/ameesh.git")):
            with self.subTest(repo=repo), self.assertRaises(packet_guard.PacketGuardError) \
                    as caught:
                packet_guard.check_merge(None, 5447, "lot/5447-x", "main", HEAD,
                                         repo=repo, cfg=self.cfg)
            self.assertIn("§11 bis", str(caught.exception))

    def test_without_a_declared_canon_the_doubt_refuses(self):
        cases = {"no cfg": None,
                 "untrusted canon": _cfg(self.canon, untrusted=True),
                 "no canon": _cfg(""),
                 "canon not a clone": _cfg(os.path.join(self.tmp, "missing")),
                 "canon without origin": _cfg(self._bare_dir())}
        for label, cfg in cases.items():
            with self.subTest(label), self.assertRaises(packet_guard.PacketGuardError):
                packet_guard.check_merge(None, 5447, "canon/5447-avis", "main", HEAD,
                                         repo="manaty/home", cfg=cfg)

    def test_an_unreadable_repository_is_refused(self):
        for repo in ("home", "not a repo", os.path.join(self.tmp, "nowhere")):
            with self.subTest(repo=repo), self.assertRaises(packet_guard.PacketGuardError):
                packet_guard.check_merge(None, 5447, "canon/5447-avis", "main", HEAD,
                                         repo=repo, cfg=self.cfg)

    def test_ci_lot_follows_the_same_repository_rule(self):
        out = packet_guard.check_ci(None, 5447, "canon/5447-avis", sha=HEAD,
                                    repo="manaty/home", cfg=self.cfg)
        self.assertTrue(out["allowed"])
        with self.assertRaises(packet_guard.PacketGuardError):
            packet_guard.check_ci(None, 5447, "lot/5447-x", sha=HEAD,
                                  repo="manaty/ameesh", cfg=self.cfg)

    def _bare_dir(self) -> str:
        path = os.path.join(self.tmp, "plain")
        os.makedirs(path)
        _git(path, "init", "-q")
        return path


class MergePrCanonDryRunTest(_Base):
    """`merge-pr --dry-run` d'une PR `canon/**` du dépôt du canon : admise
    sans exception ; la même PR, le canon configuré ailleurs : refusée."""

    LOT = 5447
    BRANCH = "canon/5447-avis"

    def setUp(self):
        super().setUp()
        _git(self.repo, "push", "-q", "origin", "lot/7-work:%s" % self.BRANCH)
        # le clone du canon de l'hôte : même `origin` que la PR
        self.canon = os.path.join(self.tmp, "canon")
        subprocess.run(["git", "clone", "-q", self.origin, self.canon], check=True,
                       capture_output=True, env=git_env())

    def test_canon_pr_is_admitted_in_dry_run(self):
        out = self.merge(self.gh(headRefName=self.BRANCH), cfg=_cfg(self.canon),
                         dry_run=True, exception="", reason="")
        self.assertEqual(out["state"], "dry-run")
        self.assertEqual(self.tip(), self.base, "rien n'a été écrit")

    def test_canon_pr_is_refused_when_the_repository_is_not_the_canon(self):
        other = _clone_with_origin(self.tmp, "other", "git@github.com:manaty/home.git")
        with self.assertRaises(Exception) as caught:
            self.merge(self.gh(headRefName=self.BRANCH), cfg=_cfg(other),
                       dry_run=True, exception="", reason="")
        self.assertIn("§11 bis", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
