# SPDX-License-Identifier: AGPL-3.0-only
"""#2673 — le clone radar déclare sa cible (`ameesh.target`).

Un clone radar est cloné depuis le checkout source : quand celui-ci est
détaché sur une release, `origin/HEAD` du clone nomme `release/*` et
`plan_git.default_target` y lit cette release. `lot_class` classait alors
« release » tout lot sans `branch_target` (13 lots le 19/10).

Les témoins ci-dessous montent un clone dont `origin/HEAD` vise réellement une
release, puis vérifient : la création écrit la cible par défaut (`origin/main`)
et non la release, un rafraîchissement répare un clone déjà créé sans cible,
la cible déclarée du projet prime, et un lot sans `branch_target` est classé
sur ses chemins — pas « release ».
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import canon, lot_class, plan_git, resolutions


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          check=True)
    return proc.stdout.strip()


def _write(repo: str, path: str, text: str) -> None:
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)


class CloneTargetTest(unittest.TestCase):
    """Un clone dont le checkout source est détaché sur `release/1.11.0`."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2673-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "source")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.name", "banc")
        _git(self.repo, "config", "user.email", "banc@example.invalid")
        _write(self.repo, "docs/readme.md", "one\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        _git(self.repo, "branch", "develop")
        _git(self.repo, "checkout", "-q", "-b", "release/1.11.0")
        _write(self.repo, "docs/readme.md", "release\n")
        _git(self.repo, "commit", "-qam", "release")
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "checkout", "-q", "-b", "lot/1")
        _write(self.repo, "docs/readme.md", "lot\n")
        _git(self.repo, "commit", "-qam", "lot")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "release/1.11.0")
        _git(self.repo, "checkout", "-q", "--detach")
        self.clone = os.path.join(self.tmp, "radar", "p.clone")
        self.replayer = resolutions.Replayer(self.repo, self.clone)

    def test_creation_declares_main_not_the_stale_release(self) -> None:
        self.replayer.ensure()
        # le clone a bien hérité du HEAD détaché du checkout source...
        self.assertEqual(
            _git(self.clone, "symbolic-ref", "refs/remotes/origin/HEAD"),
            "refs/remotes/origin/release/1.11.0")
        # ... mais sa cible déclarée ne suit plus cette ref.
        self.assertEqual(plan_git.declared_target(self.clone), "origin/main")
        self.assertEqual(plan_git.default_target(self.clone), "origin/main")

    def test_existing_clone_is_repaired_by_ensure_and_fetch(self) -> None:
        self.replayer.ensure()
        _git(self.clone, "config", "--unset", "ameesh.target")
        self.replayer.ensure()
        self.assertEqual(plan_git.declared_target(self.clone), "origin/main")
        _git(self.clone, "config", "--unset", "ameesh.target")
        self.replayer.fetch()
        self.assertEqual(plan_git.declared_target(self.clone), "origin/main")

    def test_existing_manual_target_is_never_overwritten(self) -> None:
        self.replayer.ensure()
        _git(self.clone, "config", "ameesh.target", "origin/release/1.11.0")
        # même une cible de projet différente ne remplace pas la clé posée
        resolutions.Replayer(self.repo, self.clone, target="develop").ensure()
        self.assertEqual(plan_git.declared_target(self.clone), "origin/release/1.11.0")

    def test_declared_project_target_primes_over_the_clone_head(self) -> None:
        clone = os.path.join(self.tmp, "radar", "develop.clone")
        target = resolutions.Replayer(self.repo, clone, target="develop")
        target.ensure()
        self.assertEqual(plan_git.declared_target(clone), "origin/develop")
        self.assertEqual(plan_git.default_target(clone), "origin/develop")

    def _classify(self) -> dict:
        row = {"id": 1, "app": "ameesh", "branch": "lot/1", "branch_target": "",
               "type": "evolution"}
        marks = [{"kind": "frozen", "sha": self.head}]
        with mock.patch.object(canon, "git_range_paths", return_value=["docs/readme.md"]), \
                mock.patch.object(lot_class, "class_of_paths",
                                  return_value={"class": lot_class.LIGHT,
                                                "why": "policy of test", "files": [],
                                                "policy_ref": "test", "doubt": False}) \
                as classes:
            out = lot_class.of_lot(None, 1, item=row, repo=self.clone, head=self.head,
                                   milestones=marks, canons=[object()])
        return out, classes

    def test_lot_without_target_follows_its_paths_not_the_stale_release(self) -> None:
        self.replayer.ensure()
        # état d'avant le correctif : clone créé sans cible, origin/HEAD release
        _git(self.clone, "config", "--unset", "ameesh.target")
        before, _ = self._classify()
        self.assertEqual(before["class"], lot_class.RELEASE, before)
        # le rafraîchissement écrit la cible ; la classe se lit sur le diff
        self.replayer.declare_target()
        after, classes = self._classify()
        self.assertEqual(after["class"], lot_class.LIGHT, after)
        self.assertEqual(after["target"], "origin/main")
        classes.assert_called_once()


if __name__ == "__main__":
    unittest.main()
