# SPDX-License-Identifier: AGPL-3.0-only
"""#3904: vérifier plus de 512 pointes de PR sans ignorer leur histoire."""
import os
import subprocess
import tempfile
from unittest import TestCase, mock

from ameesh import plan_git
from .test_2447_review_designation_activation import _Repo


class RemoteTipBatchTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.repo = _Repo(os.path.join(self.temp.name, "local"))
        self.repo.write("f", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("f", "ghost\n")
        self.ghost = self.repo.commit("ghost")
        self.repo.git("reset", "--hard", self.base)
        self.remote = _Repo(os.path.join(self.temp.name, "remote"))
        self.repo.git("remote", "add", "origin", self.remote.root)
        self.repo.git("push", "-q", "origin", self.base + ":refs/heads/base")
        tree = self.remote.git("rev-parse", self.base + "^{tree}")
        for n in range(513):
            tip = self.remote.git("commit-tree", tree, "-p", self.base,
                                  "-m", "pull %d" % n)
            self.remote.git("update-ref", "refs/pull/%d/head" % n, tip)

    def test_many_absent_pull_tips_prove_absence_in_one_fetch(self):
        before = self.repo.git("for-each-ref")
        with mock.patch.object(plan_git, "_git", wraps=plan_git._git) as git:
            self.assertFalse(plan_git.remote_reachable(self.repo.root, self.ghost))
        fetches = [call for call in git.call_args_list if call.args[1] == "fetch"]
        self.assertEqual(len(fetches), 1)
        self.assertEqual(len(fetches[0].kwargs["stdin"].splitlines()), 513)
        self.assertEqual(self.repo.git("for-each-ref"), before)
        self.assertFalse(os.path.exists(os.path.join(self.repo.root, ".git", "FETCH_HEAD")))

    def test_a_pull_descendant_still_keeps_the_ghost_reachable(self):
        self.repo.git("push", "-q", "origin", self.ghost + ":refs/archive/seed")
        tree = self.remote.git("rev-parse", self.ghost + "^{tree}")
        child = self.remote.git("commit-tree", tree, "-p", self.ghost, "-m", "child")
        self.remote.git("update-ref", "refs/pull/9999/head", child)
        self.remote.git("update-ref", "-d", "refs/archive/seed")
        self.assertTrue(plan_git.remote_reachable(self.repo.root, self.ghost))

    def test_exhausted_budget_or_failed_fetch_keeps_the_doubt(self):
        self.assertIsNone(plan_git.remote_reachable(
            self.repo.root, self.ghost, extra_budget=512))
        real = plan_git._git

        def fail_fetch(repo, *args, **kwargs):
            if args[0] == "fetch":
                return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"failed")
            return real(repo, *args, **kwargs)

        with mock.patch.object(plan_git, "_git", side_effect=fail_fetch):
            self.assertIsNone(plan_git.remote_reachable(self.repo.root, self.ghost))
