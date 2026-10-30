# SPDX-License-Identifier: AGPL-3.0-only
"""#3762 : les copies rebasées déjà intégrées ne portent plus d'auteur du lot."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import plan_git, review_authors

from .test_2447_review_designation_activation import _Repo
from . import test_3116_provenance_target as target_tests


class TargetPatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3762-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = _Repo(os.path.join(self.tmp, "repo"))
        self.repo.write("base.txt", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("fix.py", "if ready:\n    run()\n")
        self.original = self.repo.commit("reviewer fix", agent="cx")
        self.repo.git("checkout", "-q", "-b", "target", self.base)
        self.repo.write("anchor.txt", "another target commit\n")
        self.repo.commit("anchor")
        self.repo.git("cherry-pick", self.original)
        self.target = self.repo.git("rev-parse", "HEAD")
        self.assertNotEqual(self.original, self.target)

    def remaining(self, commits):
        return review_authors._not_in_target(self.repo.root, commits, self.target)

    def test_rebased_patch_is_excluded(self):
        self.assertEqual(self.remaining([self.original]), [])

    def test_abbreviated_patch_is_excluded(self):
        self.assertEqual(self.remaining([self.original[:12]]), [])

    def test_reverted_target_patch_keeps_author(self):
        self.repo.git("revert", "--no-edit", self.target)
        self.target = self.repo.git("rev-parse", "HEAD")
        self.assertEqual(self.remaining([self.original]), [self.original])

    def test_rewritten_target_patch_keeps_author(self):
        self.repo.write("fix.py", "different_content()\n")
        self.target = self.repo.commit("rewrite target patch")
        self.assertEqual(self.remaining([self.original]), [self.original])

    def test_replay_read_failure_keeps_author(self):
        git = plan_git._git

        def fail_merge(repo, *args, **kwargs):
            if args[0] == "merge-tree":
                raise OSError("unreadable replay")
            return git(repo, *args, **kwargs)

        with mock.patch.object(plan_git, "_git", side_effect=fail_merge):
            self.assertEqual(self.remaining([self.original]), [self.original])

    def test_patch_absent_from_target_keeps_corrector(self):
        self.repo.git("checkout", "-q", "-b", "unmerged", self.original)
        self.repo.write("guard.py", "enforce_review_independence()\n")
        fix = self.repo.commit("unmerged corrector", agent="cx")
        self.assertEqual(self.remaining([self.original, fix]), [fix])

    def test_a_candidate_descending_from_target_does_not_mask_equivalence(self):
        self.repo.write("lot.py", "own_lot_content()\n")
        own = self.repo.commit("lot on target")
        self.assertEqual(self.remaining([self.original, own]), [own])

    def test_significant_whitespace_difference_keeps_corrector(self):
        self.repo.git("checkout", "-q", "-b", "different", self.base)
        self.repo.write("fix.py", "if ready:\n  run()\n")
        fix = self.repo.commit("different indentation", agent="cx")
        self.assertEqual(self.remaining([fix]), [fix])

    def test_patch_read_failure_keeps_corrector(self):
        with mock.patch.object(review_authors, "_read_patch_ids", side_effect=RuntimeError):
            self.assertEqual(self.remaining([self.original]), [self.original])

    def test_candidate_read_failure_keeps_corrector(self):
        read = review_authors._read_patch_ids

        def fail_candidate(repo, *args):
            if "--no-walk" in args:
                raise RuntimeError("unreadable candidate")
            return read(repo, *args)

        with mock.patch.object(review_authors, "_read_patch_ids", side_effect=fail_candidate):
            self.assertEqual(self.remaining([self.original]), [self.original])

    def test_local_target_log_failure_keeps_corrector(self):
        self._local_read_failure_keeps_corrector(
            lambda args: args[0] == 'log' and '-p' in args and '--no-walk' not in args)

    def test_local_candidate_log_failure_keeps_corrector(self):
        self._local_read_failure_keeps_corrector(
            lambda args: args[0] == 'log' and '--no-walk' in args)

    def test_local_patch_id_failure_keeps_corrector(self):
        self._local_read_failure_keeps_corrector(lambda args: args[0] == 'patch-id')

    def _local_read_failure_keeps_corrector(self, fails):
        read = review_authors._read_git
        failed = []

        def fail(repo, *args, **kwargs):
            if fails(args):
                failed.append(args)
                return subprocess.CompletedProcess(args, 1, b'', b'unreadable local object')
            return read(repo, *args, **kwargs)

        with mock.patch.object(review_authors, '_read_git', side_effect=fail):
            self.assertEqual(self.remaining([self.original]), [self.original])
        self.assertTrue(failed, 'the failure must reach the local reader')

    def test_empty_commit_keeps_author(self):
        self.repo.git("checkout", "-q", "-b", "empty", self.original)
        self.repo.git("commit", "--allow-empty", "-qm", "empty")
        empty = self.repo.git("rev-parse", "HEAD")
        self.assertEqual(self.remaining([empty]), [empty])

    def test_merge_commit_keeps_author(self):
        self.repo.git("checkout", "-q", "-b", "merge", self.original)
        self.repo.git("merge", "--no-ff", "-qm", "merge target", self.target)
        merge = self.repo.git("rev-parse", "HEAD")
        self.assertEqual(self.remaining([merge]), [merge])

    def test_target_scan_limit_keeps_unproven_author(self):
        self.repo.write("recent.txt", "newer target content\n")
        self.target = self.repo.commit("recent")
        with mock.patch.object(plan_git, "MAX_TARGET_COMMITS", 1):
            self.assertEqual(self.remaining([self.original]), [self.original])


class RebasedTargetAuthorsTest(target_tests.TargetCommitTest):
    """Même preuve partagée porte/désignation, cible intégrée sous d'autres SHA.

    Les tests hérités couvrent plage courante, report des gels antérieurs,
    lecture stricte de la porte et correcteur absent de la cible.
    """
    def setUp(self):
        super().setUp()
        self.repo.git("checkout", "-q", "-b", "target-rebased", self.base)
        self.repo.write("anchor.txt", "target advanced before replay\n")
        self.repo.commit("anchor")
        self.repo.git("cherry-pick", self.target1, self.target)
        rebased = self.repo.git("rev-parse", "HEAD")
        self.assertNotEqual(rebased, self.target)
        self.target = rebased


if __name__ == "__main__":
    unittest.main()
