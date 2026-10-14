# SPDX-License-Identifier: AGPL-3.0-only
"""A manual merge declaration binds local Git evidence to the work item."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import support  # isolate host config before importing work
from ameesh import work


class DeclaredMergeProofTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="merge-proof-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", "git@github.com:acme/nexlink.git")
        self.item = {"id": 637, "app": "nexlink", "title": "T3 redemption expiry",
                     "state": "build", "issue_ref": "acme/nexlink#12", "branch": None,
                     "branch_target": None}
        self.commit("Base")

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
                   GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid")
        proc = subprocess.run(["git", "-C", self.repo, "-c", "core.hooksPath=" + os.devnull,
                               *args], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def commit(self, message):
        self.git("commit", "--allow-empty", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def declare(self, sha):
        with mock.patch.object(work, "get", return_value=self.item), \
                mock.patch.object(work, "close_merged", return_value={
                    "result": "merged", "item": self.item}) as close:
            result = work.merged(object(), 637, sha=sha, repo=self.repo, actor="cx3")
            return result, close.call_args.kwargs

    def refused(self, sha, message):
        with mock.patch.object(work, "get", return_value=self.item), \
                mock.patch.object(work, "close_merged") as close:
            with self.assertRaisesRegex(work.WorkError, message) as error:
                work.merged(object(), 637, sha=sha, repo=self.repo)
            self.assertIn("nexlink: T3 redemption expiry", str(error.exception))
            close.assert_not_called()

    def test_canonical_sha_for_commit_designating_the_lot(self):
        sha = self.commit("Deliver\n\nameesh-work: 637")
        _, args = self.declare(sha[:9].upper())
        self.assertEqual(args["sha"], sha)
        self.assertEqual(args["actor"], "cx3")
        self.assertTrue(args["declared"])

    def test_homonymous_pr_in_foreign_repository_is_refused(self):
        sha = self.commit("Merge pull request #637\n\nameesh-work: 834")
        self.git("remote", "set-url", "origin", "git@github.com:acme/ameesh.git")
        self.refused(sha, "not of acme/nexlink")

    def test_number_of_pr_alone_is_not_a_lot_reference(self):
        self.refused(self.commit("Merge pull request #637"), "does not designate")

    def test_foreign_lot_marker_in_same_repository_is_refused(self):
        self.refused(self.commit("Deliver\n\nameesh-work: 834"), "does not designate")

    def test_unreadable_sha_is_refused(self):
        self.refused("f" * 40, "not found")

    def test_unmerged_commit_is_refused_even_with_correct_marker(self):
        self.git("checkout", "-qb", "pending")
        sha = self.commit("Deliver\n\nameesh-work: 637")
        self.refused(sha, "has not entered target main")

    def test_partial_marker_vetoes_complete_marker(self):
        self.refused(self.commit("Deliver\n\nameesh-work: 637\nameesh-work-partial: 637"),
                     "partial delivery")

    def test_hash_title_requires_repository_opt_in(self):
        sha = self.commit("Merge #637 redemption")
        self.refused(sha, "does not designate")
        self.git("config", "ameesh.lotRef", "hash")
        self.declare(sha)

    def test_missing_configured_target_is_not_silently_main(self):
        sha = self.commit("Deliver\n\nameesh-work: 637")
        self.git("config", "ameesh.target", "origin/missing")
        self.refused(sha, "not found")

    def test_work_item_target_overrides_repository_default(self):
        sha = self.commit("Deliver\n\nameesh-work: 637")
        self.item["branch_target"] = "integration"
        self.refused(sha, "target 'integration' not found")
        self.git("branch", "integration", sha)
        self.declare(sha)

    def test_no_repository_identity_is_refused(self):
        sha = self.commit("Deliver\n\nameesh-work: 637")
        self.item["issue_ref"] = None
        self.refused(sha, "no repository")

    def test_recorded_branch_must_match_merge_title(self):
        sha = self.commit("Merge branch 'lot/637-other'")
        self.item["branch"] = "lot/637-real"
        self.refused(sha, "does not designate")

    def test_recorded_branch_is_accepted_in_a_real_merge(self):
        self.item["branch"] = "agent/cx3-redemption"
        self.git("checkout", "-qb", self.item["branch"])
        Path(self.repo, "delivered.txt").write_text("work\n")
        self.git("add", "delivered.txt")
        self.commit("Work")
        self.git("checkout", "-q", "main")
        self.git("merge", "--no-ff", self.item["branch"], "-qm",
                 "Merge branch 'agent/cx3-redemption'")
        self.declare(self.git("rev-parse", "HEAD"))

    def test_branch_mention_in_a_review_body_proves_nothing(self):
        self.item["branch"] = "agent/cx3-redemption"
        self.refused(self.commit("Review\n\nRead agent/cx3-redemption"), "does not designate")

    def test_project_configuration_supplies_repository_and_checkout(self):
        sha = self.commit("Deliver\n\nameesh-work: 637")
        self.item["issue_ref"] = None
        cfg = SimpleNamespace(github={"projects": {"nexlink": "acme/nexlink"}},
                              radar={"projects": {"nexlink": {
                                  "repo": self.repo, "github": "acme/nexlink"}}})
        self.assertEqual(work._declared_merge_commit(self.item, sha, cfg=cfg), sha)

    def test_empty_message_is_refused_with_lot_diagnostic(self):
        self.item["branch"] = "lot/637-x"
        self.git("commit", "--allow-empty", "--allow-empty-message", "-qm", "")
        self.refused(self.git("rev-parse", "HEAD"), "does not designate")


if __name__ == "__main__":
    unittest.main()
