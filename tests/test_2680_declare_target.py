# SPDX-License-Identifier: AGPL-3.0-only
"""#2680 — la cible déclarée du clone radar ne s'écrit que si elle se résout.

Deux N de la relecture de #2673 :

- N1 : `Replayer._declared_ref` rendait `self.target` même quand la ref ne se
  résolvait pas dans le clone. La clé n'étant jamais écrasée, la cible absente
  restait posée et `default_target` levait `MergeProbeError` au lieu de
  retomber sur `origin/HEAD`/main. Attendu : ne rien écrire dans ce cas.
- N2 : `fetch` déclare la cible hors verrou ; deux fetch concurrents peuvent
  se heurter au verrou de `git config` (`.git/config.lock`) à la première
  écriture. Attendu : tolérer cet échec transitoire, la passe suivante réessaie.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import plan_git, resolutions


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          check=True)
    return proc.stdout.strip()


def _write(repo: str, path: str, text: str) -> None:
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)


class DeclareTargetTest(unittest.TestCase):
    """Un clone dont le checkout source est détaché sur `release/1.11.0`."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2680-")
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
        _git(self.repo, "checkout", "-q", "release/1.11.0")
        _git(self.repo, "checkout", "-q", "--detach")
        self.clone = os.path.join(self.tmp, "radar", "p.clone")
        self.replayer = resolutions.Replayer(self.repo, self.clone)

    def _unset(self) -> None:
        _git(self.clone, "config", "--unset", plan_git.TARGET_GIT_KEY)

    def test_unresolvable_project_target_is_not_written(self) -> None:
        self.replayer = resolutions.Replayer(self.repo, self.clone, target="ghost")
        self.assertEqual(self.replayer.declare_target(), "")  # clone absent : rien
        self.replayer.ensure()
        self.assertIsNone(plan_git.declared_target(self.clone))
        # pas de clé irrésolue : le repli du clone reste lisible
        self.assertEqual(plan_git.default_target(self.clone),
                         "origin/release/1.11.0")

    def test_fetch_does_not_write_an_unresolvable_target(self) -> None:
        self.replayer.ensure()  # crée le clone et pose origin/main
        self._unset()
        self.assertEqual(resolutions.Replayer(
            self.repo, self.clone, target="ghost").declare_target(), "")
        resolutions.Replayer(self.repo, self.clone, target="ghost").fetch()
        self.assertIsNone(plan_git.declared_target(self.clone))
        self.assertEqual(plan_git.default_target(self.clone),
                         "origin/release/1.11.0")

    def test_fetch_tolerates_the_git_config_lock(self) -> None:
        self.replayer.ensure()
        self._unset()
        lock = os.path.join(self.clone, ".git", "config.lock")
        with open(lock, "w", encoding="utf-8"):
            pass
        try:
            # Deux fetch concurrents : l'un tient le verrou de `git config`
            # pendant que l'autre déclare la cible. Aucun ne doit échouer.
            self.replayer.fetch()
        finally:
            os.unlink(lock)
        self.assertIsNone(plan_git.declared_target(self.clone))
        # la passe suivante réessaie et déclare la cible
        self.replayer.fetch()
        self.assertEqual(plan_git.declared_target(self.clone), "origin/main")

    def test_fetch_still_raises_another_config_error(self) -> None:
        self.replayer.ensure()
        self._unset()
        with mock.patch.object(resolutions.Replayer, "declare_target",
                               side_effect=resolutions.GitError(
                                   "git config: permission denied")):
            with self.assertRaises(resolutions.GitError):
                self.replayer.fetch()


if __name__ == "__main__":
    unittest.main()
