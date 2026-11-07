# SPDX-License-Identifier: AGPL-3.0-only
"""#6161 : le gel d'un panier reçoit une branche publiée sans ambiguïté."""
from __future__ import annotations

import os
import shutil
import tempfile
from unittest import mock

from ameesh import backlog, freeze_branch, work
from .support import PgTestCase
from .test_2962_freeze_unresolved import author_with_forge, git


class FreezeBranchTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="ameesh-6161-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.forge, self.repo = author_with_forge(self.root)
        git(self.repo, "checkout", "-qb", "agent/basket")
        with open(os.path.join(self.repo, "change"), "w", encoding="utf-8") as stream:
            stream.write("change\n")
        git(self.repo, "add", "change")
        git(self.repo, "commit", "-qm", "change")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.lot = backlog.add(self.db, title="panier", value_score=40, expected_value="gel visible")["id"]

    def publish(self, name="agent/basket"):
        git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/" + name)

    def freeze(self, hook=None):
        return work.milestone(self.db, self.lot, "frozen", sha=self.sha,
                              repo=self.repo, actor="human:test", on_frozen=hook)

    def test_published_basket_is_associated_and_journalled(self):
        self.publish()
        seen = []
        self.freeze(lambda tx, mark, after: seen.append(work.get(tx, self.lot)["branch"]))
        self.assertEqual(seen, ["agent/basket"])
        self.assertEqual(work.get(self.db, self.lot)["branch"], "agent/basket")
        self.assertTrue(any("inferred at freeze" in e["note"]
                            for e in work.events(self.db, self.lot)))

    def test_two_published_tips_refuse_without_writing(self):
        self.publish()
        self.publish("agent/other")
        with self.assertRaisesRegex(work.WorkError, "branch-ambiguous"):
            self.freeze()
        self.assertFalse(any(m["kind"] == "frozen" for m in work.milestones(self.db, self.lot)))
        self.assertIsNone(work.get(self.db, self.lot)["branch"])

    def test_local_or_stale_remote_tip_does_not_count(self):
        self.publish()
        git(self.repo, "push", "-q", "origin", ":refs/heads/agent/basket")
        # Même une ref de suivi périmée ne prouve pas la publication.
        git(self.repo, "update-ref", "refs/remotes/origin/agent/basket", self.sha)
        row = self.freeze()
        self.assertIn(freeze_branch.MISSING_MARK, row["note"])
        self.assertIsNone(work.get(self.db, self.lot)["branch"])

    def test_existing_branch_does_not_need_inference(self):
        work.set_branch(self.db, self.lot, "explicit", actor="human:test")
        with mock.patch.object(freeze_branch, "published_branch") as search:
            self.freeze()
        search.assert_not_called()
        self.assertEqual(work.get(self.db, self.lot)["branch"], "explicit")

    def test_failed_hook_rolls_back_branch_and_freeze(self):
        self.publish()
        def fail(tx, mark, after):
            raise work.WorkError("test rollback")
        with self.assertRaisesRegex(work.WorkError, "test rollback"):
            self.freeze(fail)
        self.assertIsNone(work.get(self.db, self.lot)["branch"])
        self.assertFalse(any(m["kind"] == "frozen" for m in work.milestones(self.db, self.lot)))

    def test_detached_branch_is_not_reattached(self):
        self.publish()
        work.set_branch(self.db, self.lot, "agent/basket", actor="human:test")
        work.detach_branch(self.db, self.lot, reason="explicit choice", actor="human:test")
        with self.assertRaisesRegex(work.WorkError, "branch-not-associated"):
            self.freeze()
        self.assertIsNone(work.get(self.db, self.lot)["branch"])

    def test_concurrent_explicit_branch_is_not_overwritten(self):
        self.publish()
        lookup = freeze_branch.published_branch
        def associate(repo, sha):
            result = lookup(repo, sha)
            work.set_branch(self.db, self.lot, "explicit", actor="human:test")
            return result
        with mock.patch.object(freeze_branch, "published_branch", side_effect=associate):
            with self.assertRaisesRegex(work.WorkError, "branch-not-associated"):
                self.freeze()
        self.assertEqual(work.get(self.db, self.lot)["branch"], "explicit")
        self.assertFalse(any(m["kind"] == "frozen" for m in work.milestones(self.db, self.lot)))

    def test_missing_repository_warns(self):
        branch, warning = freeze_branch.published_branch("", self.sha)
        self.assertIsNone(branch)
        self.assertIn(freeze_branch.MISSING_MARK, warning)

    def test_remote_unavailable_warns(self):
        git(self.repo, "remote", "set-url", "origin", os.path.join(self.root, "absent.git"))
        branch, warning = freeze_branch.published_branch(self.repo, self.sha)
        self.assertIsNone(branch)
        self.assertIn("remote heads unavailable", warning)
