# SPDX-License-Identifier: AGPL-3.0-only
"""#3318 — local names must not hide the remote provenance target."""
import os
import tempfile
import unittest

from . import support  # isolate the runner environment
from .test_2447_review_designation_activation import _Repo
from ameesh import review_authors


class RemoteTargetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = _Repo(os.path.join(self.tmp.name, "repo"))
        self.repo.git("remote", "add", "origin", "https://example.invalid/repo")
        self.repo.write("base.txt", "merged target\n")
        self.target = self.repo.commit("merged target")
        self.repo.write("fix.txt", "unmerged corrector\n")
        self.fix = self.repo.commit("unmerged fix")
        self.repo.git("update-ref", "refs/remotes/origin/main", self.target)
        self.repo.git("update-ref", "refs/heads/main", self.fix)

    def test_local_branch_cannot_hide_remote_or_remove_corrector(self):
        self.repo.git("update-ref", "refs/heads/origin/main", self.fix)
        self.check_remote()

    def test_local_tag_cannot_hide_remote_or_remove_corrector(self):
        self.repo.git("update-ref", "refs/tags/origin/main", self.fix)
        self.check_remote()

    def check_remote(self):
        for ref in ("main", "origin/main", "refs/remotes/origin/main"):
            with self.subTest(ref=ref):
                target = review_authors.target_commit(self.repo.root, ref)
                self.assertEqual(target, self.target)
                self.assertEqual(review_authors._not_in_target(
                    self.repo.root, [self.target, self.fix], target), [self.fix])

    def test_explicit_refs_are_read_exactly(self):
        self.repo.git("update-ref", "refs/tags/main", self.fix)
        for ref in ("refs/heads/main", "refs/tags/main", self.fix):
            with self.subTest(ref=ref):
                self.assertEqual(review_authors.target_commit(self.repo.root, ref),
                                 self.fix)

    def test_missing_explicit_ref_does_not_fall_back_to_remote(self):
        self.assertEqual(review_authors.target_commit(
            self.repo.root, "refs/heads/origin/main"), "")
