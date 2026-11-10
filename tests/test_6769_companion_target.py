# SPDX-License-Identifier: AGPL-3.0-only
"""#6769 — la cible de relecture d'un dépôt compagnon n'est pas celle du projet.

Le lot nexlink #6746 porte une PR de manaty/manaty-its ; sans `branch_target`,
la désignation au gel héritait de `develop` (cible du projet Nexlink) et la
fetchait dans le clone manaty-its, qui n'a que `main` : « cannot refresh target
develop: git fetch failed; stale target refused » (gels #38052, #38054).

Attendu : le dépôt du projet est celui que la configuration déclare (jamais
l'issue ni la PR) ; un compagnon prend sa cible déclarée (projet qui le
déclare, puis `git config ameesh.target`), sinon un refus nommé, jamais
`origin/HEAD` deviné ; lots ordinaires, forks et cibles explicites inchangés.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import lot_class
from ameesh import review_designation_activation as activation


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _clone(root: str, name: str, origin: str, branches) -> str:
    """Un clone d'origine GitHub `origin`, `origin/HEAD` sur `branches[0]`."""
    repo = os.path.join(root, name)
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", branches[0])
    _git(repo, "config", "user.name", "banc")
    _git(repo, "config", "user.email", "banc@example.invalid")
    _git(repo, "config", "remote.origin.url", "https://github.com/%s.git" % origin)
    _git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    with open(os.path.join(repo, "f.txt"), "w", encoding="utf-8") as handle:
        handle.write(name + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    for branch in branches:
        _git(repo, "update-ref", "refs/remotes/origin/%s" % branch, "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD",
         "refs/remotes/origin/%s" % branches[0])
    return repo


class CompanionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-6769-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # projet : develop déclaré, origin/HEAD = main ; compagnon : main seul
        self.project = _clone(self.tmp, "nexlink", "manaty/nexlink", ["main", "develop"])
        self.companion = _clone(self.tmp, "manaty-its-claude2-6746", "manaty/manaty-its",
                                ["main"])

    def cfg(self, **extra):
        values = {"github": {"projects": {"nexlink": {
            "repo": "manaty/nexlink", "base": ["develop"]}}}}
        values.update(extra)
        return types.SimpleNamespace(**values)

    def item(self, **extra):
        # l'issue est celle du projet, la PR celle du compagnon (#6746)
        row = {"id": 6746, "app": "nexlink", "branch": "review/claude2-6746",
               "branch_target": None, "issue_ref": "manaty/nexlink#4111"}
        row.update(extra)
        return row


class DefaultBaseTest(CompanionFixture):
    def test_project_clone_keeps_the_project_target(self):
        self.assertEqual(lot_class.default_base(self.cfg(), self.item(), self.project),
                         "develop")
        self.assertEqual(lot_class.companion_repository(self.cfg(), self.item(),
                                                        self.project), "")

    def test_companion_does_not_inherit_develop_nor_guess_origin_head(self):
        cfg = self.cfg()
        self.assertEqual(lot_class.companion_repository(cfg, self.item(), self.companion),
                         "manaty/manaty-its")
        self.assertIsNone(lot_class.default_base(cfg, self.item(), self.companion))
        self.assertIsNone(lot_class.default_base(cfg, self.item(), self.companion,
                                                 declared_only=True))

    def test_companion_reads_its_clone_declared_target(self):
        _git(self.companion, "config", "ameesh.target", "origin/main")
        self.assertEqual(lot_class.default_base(self.cfg(), self.item(), self.companion),
                         "origin/main")

    def test_companion_reads_the_project_declaring_its_repository(self):
        cfg = self.cfg()
        cfg.github["projects"]["manaty-its"] = {"repo": "manaty/manaty-its",
                                                "base": ["main"]}
        self.assertEqual(lot_class.default_base(cfg, self.item(), self.companion), "main")

    def test_explicit_lot_target_is_preserved(self):
        self.assertEqual(lot_class.default_base(self.cfg(), self.item(branch_target="main"),
                                                self.companion), "main")

    def test_fork_of_the_project_repository_is_not_a_companion(self):
        fork = _clone(self.tmp, "fork", "someone/nexlink", ["main", "develop"])
        self.assertEqual(lot_class.default_base(self.cfg(), self.item(), fork), "develop")

    def test_no_declared_project_repository_keeps_the_project_target(self):
        cfg = types.SimpleNamespace(release={"projects": {"nexlink": {"target": "develop"}}})
        self.assertEqual(lot_class.default_base(cfg, self.item(), self.companion), "develop")

    def test_issue_repository_is_not_the_project_repository(self):
        # une issue ouverte dans le compagnon ne fait pas du projet un compagnon
        item = self.item(issue_ref="manaty/manaty-its#5")
        self.assertEqual(lot_class.default_base(self.cfg(), item, self.project), "develop")
        self.assertIsNone(lot_class.default_base(self.cfg(), item, self.companion))


class PrepareTargetTest(CompanionFixture):
    def prepare(self, repo, item=None):
        head = _git(repo, "rev-parse", "HEAD")
        with mock.patch.object(activation, "_repository", return_value=repo), \
                mock.patch.object(activation.review_target, "resolve",
                                  return_value=head) as resolve:
            got = activation.prepare_target(self.cfg(), None, item or self.item(), head)
        return got, resolve

    def test_companion_without_target_is_a_named_refusal_without_fetch(self):
        got, resolve = self.prepare(self.companion)
        self.assertIsInstance(got, activation.Unavailable)
        self.assertIn("manaty/manaty-its is a companion repository of project nexlink",
                      got.why)
        self.assertIn("develop is not inherited", got.why)
        self.assertIn("ameesh.target", got.why)
        resolve.assert_not_called()

    def test_companion_with_declared_target_resolves_it(self):
        _git(self.companion, "config", "ameesh.target", "origin/main")
        got, resolve = self.prepare(self.companion)
        self.assertIsInstance(got, activation.Target)
        self.assertEqual(got.declared, "origin/main")
        self.assertEqual(resolve.call_args.args[1], "origin/main")

    def test_project_clone_still_resolves_develop(self):
        got, resolve = self.prepare(self.project)
        self.assertEqual(got.declared, "develop")
        self.assertEqual(resolve.call_args.args[1], "develop")


if __name__ == "__main__":
    unittest.main()
