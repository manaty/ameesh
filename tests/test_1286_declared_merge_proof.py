# SPDX-License-Identifier: AGPL-3.0-only
"""A manual merge declaration binds local Git evidence to the work item."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import support  # isolate host config before importing work
from ameesh import land, plan_git, work


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

    def test_published_target_wins_over_lagging_local_branch(self):
        self.git("branch", "develop")
        sha = self.commit("Deliver\n\nameesh-work: 637")
        self.git("update-ref", "refs/remotes/origin/develop", sha)
        self.item["branch_target"] = "develop"
        self.declare(sha)
        self.assertTrue(plan_git.probe(self.repo, sha, "develop")["merged"])
        self.assertTrue(plan_git._content_ancestor(self.repo, sha, "develop"))
        absent = self.commit("Not published\n\nameesh-work: 637")
        self.refused(absent, "has not entered target develop")
        self.assertFalse(plan_git.probe(self.repo, absent, "develop")["merged"])
        self.assertFalse(plan_git._content_ancestor(self.repo, absent, "develop"))

    def test_explicit_local_target_is_not_replaced_by_remote(self):
        self.git("branch", "develop")
        sha = self.commit("Deliver\n\nameesh-work: 637")
        self.git("update-ref", "refs/remotes/origin/develop", sha)
        self.item["branch_target"] = "refs/heads/develop"
        self.refused(sha, "has not entered target refs/heads/develop")
        for target in ("origin/develop", "refs/remotes/origin/develop"):
            with self.subTest(target=target):
                self.item["branch_target"] = target
                self.declare(sha)

    def test_published_target_behind_local_refuses_unpublished_commit(self):
        self.git("update-ref", "refs/remotes/origin/develop", "HEAD")
        sha = self.commit("Deliver\n\nameesh-work: 637")
        self.git("branch", "develop", sha)
        self.item["branch_target"] = "develop"
        self.refused(sha, "has not entered target develop")

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

    # -- #2603 : une PR fusionnée hors du dépôt du lot ----------------------

    def pr_view(self, **over):
        data = {"number": 112, "state": "MERGED",
                "title": "canon: mécanique et train_reviews",
                "mergeCommit": {"oid": "b" * 40},
                "headRefOid": "a" * 40, "headRefName": "canon/2452",
                "baseRefName": "main",
                "url": "https://github.com/acme/home/pull/112"}
        data.update(over)
        return subprocess.CompletedProcess(["gh"], 0, json.dumps(data), "")

    def _pr_mocks(self, freeze, *, default="main", compare="behind",
                  repo_rc=0, compare_rc=0, repo_view=None, compare_view=None,
                  **over):
        item = dict(self.item, branch="canon/2452")
        marks = ([{"id": 1, "kind": "frozen", "sha": freeze, "at_ts": 1.0}]
                 if freeze else [])

        def gh(argv, *args, **kwargs):
            if argv[:2] == ["pr", "view"]:
                return self.pr_view(**over)
            if argv[:2] == ["repo", "view"]:
                if repo_rc:
                    return subprocess.CompletedProcess(argv, repo_rc, "", "repo boom")
                payload = (repo_view if repo_view is not None
                           else ({"defaultBranchRef": {"name": default}}
                                 if default is not None else {}))
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            if argv[:1] == ["api"]:
                if compare_rc:
                    return subprocess.CompletedProcess(argv, compare_rc, "", "compare boom")
                payload = (compare_view if compare_view is not None
                           else {"status": compare})
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            raise AssertionError("unexpected gh call: %r" % (argv,))

        return item, [
            mock.patch.object(work, "get", return_value=item),
            mock.patch.object(work, "milestones", return_value=marks),
            mock.patch.object(land, "_gh", side_effect=gh),
        ]

    def declare_pr(self, *, freeze="a" * 40, sha="", **over):
        item, patches = self._pr_mocks(freeze, **over)
        with patches[0], patches[1], patches[2], \
                mock.patch.object(work, "close_merged", return_value={
                    "result": "merged", "item": item}) as close:
            result = work.merged(object(), 637, pr="acme/home#112", sha=sha, actor="cx3")
        return result, close.call_args.kwargs

    def refused_pr(self, message, *, freeze="a" * 40, sha="", **over):
        _item, patches = self._pr_mocks(freeze, **over)
        with patches[0], patches[1], patches[2], \
                mock.patch.object(work, "close_merged") as close:
            with self.assertRaisesRegex(work.WorkError, message):
                work.merged(object(), 637, pr="acme/home#112", sha=sha)
            close.assert_not_called()

    def test_a_merged_pr_of_another_repository_is_recorded(self):
        result, args = self.declare_pr()
        self.assertEqual(result["sha"], "b" * 40)
        self.assertEqual(args["sha"], "b" * 40)
        self.assertEqual(args["pr_ref"], "acme/home#112")
        self.assertTrue(args["declared"])
        self.assertEqual(args["frozen_id"], 1)
        self.assertFalse(args["unfrozen"])

    def test_without_a_freeze_the_recorded_branch_links_the_pr(self):
        result, args = self.declare_pr(freeze="")
        self.assertEqual(result["sha"], "b" * 40)
        self.assertEqual(args["pr_ref"], "acme/home#112")
        self.assertIsNone(args["frozen_id"])
        self.assertTrue(args["unfrozen"])

    def test_a_freeze_during_pr_declaration_does_not_report_success(self):
        for freeze in ("a" * 40, ""):
            with self.subTest(initial_freeze=bool(freeze)):
                item, patches = self._pr_mocks(freeze)

                def concurrent_freeze(*args, **kwargs):
                    # The transaction refuses only when given the evidence
                    # snapshot: otherwise a newer freeze would be closed.
                    if kwargs.get("frozen_id") == 1 or kwargs.get("unfrozen"):
                        return {"result": "refrozen", "item": item,
                                "detail": "new freeze declared in the meantime"}
                    return {"result": "merged", "item": item}

                with patches[0], patches[1], patches[2], \
                        mock.patch.object(work, "close_merged", side_effect=concurrent_freeze):
                    with self.assertRaisesRegex(work.WorkError, "new freeze"):
                        work.merged(object(), 637, pr="acme/home#112")

    def test_an_unmerged_pr_is_refused(self):
        self.refused_pr("not merged", state="OPEN")

    def test_a_pr_head_off_the_freeze_is_refused(self):
        self.refused_pr("frozen on", headRefOid="c" * 40)

    def test_without_a_freeze_the_pr_head_branch_must_be_the_lot_branch(self):
        self.refused_pr("head branch is", freeze="", headRefName="autre")

    def test_a_sha_that_contradicts_the_pr_is_refused(self):
        self.refused_pr("differs from the PR merge commit", sha="d" * 40)

    def test_a_missing_merge_commit_is_refused_even_with_sha(self):
        """cx2 B #2 : GitHub rend `merge_commit_sha` même en squash/rebase ;
        un --sha déclaré ne remplace pas la preuve du commit de fusion."""
        self.refused_pr("no merge commit", mergeCommit=None, sha="d" * 40)

    def test_a_reported_squash_merge_commit_is_recorded(self):
        result, args = self.declare_pr(mergeCommit={"oid": "d" * 40}, sha="d" * 40)
        self.assertEqual(result["sha"], "d" * 40)
        self.assertEqual(args["sha"], "d" * 40)
        self.assertEqual(args["pr_ref"], "acme/home#112")

    def test_a_sha_equal_to_the_merge_commit_is_accepted(self):
        result, args = self.declare_pr(sha="b" * 40)
        self.assertEqual(result["sha"], "b" * 40)
        self.assertEqual(args["sha"], "b" * 40)

    def test_a_merge_commit_outside_the_default_branch_is_refused(self):
        """cx2 B #1 : une PR fusionnée sur une base morte ne prouve pas la
        livraison ; le contenu n'est pas entré dans le défaut (home#112/#113)."""
        self.refused_pr("content not delivered", baseRefName="canon/already-merged",
                        compare="ahead")

    def test_a_dead_base_later_merged_into_default_passes(self):
        """Une base morte ensuite refusionnée dans le défaut passe : le commit
        de fusion y est alors ancêtre (`behind`)."""
        result, args = self.declare_pr(baseRefName="canon/already-merged",
                                       compare="behind")
        self.assertEqual(result["sha"], "b" * 40)
        self.assertEqual(args["pr_ref"], "acme/home#112")

    def test_an_identical_comparison_passes(self):
        result, _args = self.declare_pr(compare="identical")
        self.assertEqual(result["sha"], "b" * 40)

    def test_an_unreadable_default_branch_is_refused(self):
        self.refused_pr("default branch", repo_rc=1)

    def test_a_missing_default_branch_is_refused(self):
        self.refused_pr("default branch", default=None)

    def test_an_unreadable_comparison_is_refused(self):
        self.refused_pr("comparison", compare_rc=1)

    def test_an_unknown_comparison_status_is_refused(self):
        self.refused_pr("content not delivered", compare="diverged")

    def test_a_pr_without_freeze_nor_branch_is_refused(self):
        item = dict(self.item, branch=None)
        with mock.patch.object(work, "get", return_value=item), \
                mock.patch.object(work, "milestones", return_value=[]), \
                mock.patch.object(land, "_gh", return_value=self.pr_view()), \
                mock.patch.object(work, "close_merged") as close:
            with self.assertRaisesRegex(work.WorkError, "neither a freeze nor a branch"):
                work.merged(object(), 637, pr="acme/home#112")
            close.assert_not_called()

    def test_a_pr_ref_without_a_number_is_refused(self):
        with mock.patch.object(work, "get", return_value=self.item), \
                mock.patch.object(work, "close_merged") as close:
            with self.assertRaisesRegex(work.WorkError, "owner/repo#N"):
                work.merged(object(), 637, pr="acme/home")
            close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
