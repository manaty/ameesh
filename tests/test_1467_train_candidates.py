# SPDX-License-Identifier: AGPL-3.0-only
"""Candidate inventory does not grant admission or create queue generations."""
import copy
import dataclasses
import os
import subprocess
import types
import unittest
from unittest import mock

from ameesh import resolutions, storage, train, train_admissions, train_candidates, train_git
from .support import PgTestCase
from . import test_1467_train_history as history_tests


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.project = types.SimpleNamespace(name="nexlink", repo="/unused", target="develop")
        self.ops = mock.Mock()
        self.item = dict(id=2005, app="nexlink", state="qa", branch="agent/member",
                         branch_target="origin/develop", type="improvement", title="member")
        self.ops.items.return_value = [self.item]
        self.marks = [dict(id=1, kind="frozen", sha="a" * 40)]
        self.ops.milestones.return_value = self.marks
        self.result = dict(sha="a" * 40, origin="frozen", declared=self.marks[0],
                           verdict=None, notes=["no-verdict"], causes=[])
        self.reader = mock.Mock(return_value=self.result)
        patch = mock.patch.object(storage, "of", return_value=types.SimpleNamespace(work=self.ops))
        patch.start()
        self.addCleanup(patch.stop)
        source = mock.patch.object(train_candidates.merge_queue, "source_of",
                                   return_value="github.com/fixture/nexlink")
        source.start()
        self.addCleanup(source.stop)
        self.inventory = train_candidates.Inventory(None, self.project, candidate=self.reader)

    def test_complete_reads_preserve_candidate_diagnostics_without_writes_or_ci(self):
        before = copy.deepcopy((self.item, self.marks, self.result))
        result = self.inventory.read()
        self.assertEqual(result["candidates"][0]["candidate"], self.result)
        self.assertEqual(result["candidates"][0]["target"], "develop")
        self.assertEqual(result["candidates"][0]["causes"], [])
        self.assertEqual([call[0] for call in self.ops.mock_calls], ["items", "milestones"])
        self.ops.items.assert_called_once_with(state="qa", assignee=None, limit=None)
        self.ops.milestones.assert_called_once_with(2005, None)
        self.reader.assert_called_once_with(self.marks)
        self.assertEqual((self.item, self.marks, self.result), before)
        self.assertNotIn("generation", result["candidates"][0])

    def test_other_projects_are_not_read_and_own_target_or_branch_failures_are_named(self):
        self.ops.items.return_value = [dict(self.item, app="other"),
                                      dict(self.item, id=2006, branch="", branch_target="main")]
        rows = self.inventory.read()["candidates"]
        self.assertEqual([row["work_item_id"] for row in rows], [2006])
        self.assertEqual(rows[0]["causes"], ["missing-branch", "target-mismatch"])
        self.ops.milestones.assert_called_once_with(2006, None)

    def test_release_and_provider_refusal_remain_named_candidates(self):
        self.item["branch_target"] = "release/1.0"
        self.result.update(sha="", causes=["blocked-verdict"])
        row = self.inventory.read()["candidates"][0]
        self.assertIn("blocked-verdict", row["causes"])
        self.assertIn("target-mismatch", row["causes"])
        self.assertTrue(any(cause.startswith("release: ") for cause in row["causes"]))
        self.assertEqual(self.result["causes"], ["blocked-verdict"])

    def test_missing_or_unusable_candidate_provider_is_not_an_empty_inventory(self):
        with self.assertRaisesRegex(train.TrainError, "explicit freeze_candidate"):
            train_candidates.Inventory(None, self.project, candidate=None)
        self.reader.return_value = None
        with self.assertRaisesRegex(train.TrainError, "lot #2005"):
            self.inventory.read()


class CompleteInventoryStorageTest(PgTestCase):
    def test_unbounded_items_read_keeps_qa_members_beyond_the_ui_page(self):
        self.db.execute("INSERT INTO work_items(title, app, state)"
                        " SELECT 'candidate-' || n, 'inventory-fixture', 'qa'"
                        " FROM generate_series(1, 205) n")
        ops = storage.of(self.db).work
        self.assertEqual(len(ops.items(state="qa", assignee=None, limit=200)), 200)
        self.assertEqual(len(ops.items(state="qa", assignee=None, limit=None)), 205)


class PreparationTest(unittest.TestCase):
    git = history_tests.RecordedHistoryTest.git
    commit = history_tests.RecordedHistoryTest.commit

    def setUp(self):
        history_tests.RecordedHistoryTest.setUp(self)
        self.git("branch", "main", self.base)
        self.git("branch", "lot/member", self.head)
        origin = os.path.join(self.path, "origin.git")
        subprocess.run(["git", "clone", "-q", "--bare", self.path, origin],
                       check=True, capture_output=True)
        self.git("remote", "add", "origin", origin)
        self.project = types.SimpleNamespace(name="nexlink", repo=self.path, target="main")
        clone = resolutions.Replayer(self.path, os.path.join(self.path, "private.clone"))
        self.repository = train_git.Repository(self.project, clone)
        self.history.path = clone.path
        self.item = dict(id=1, app="nexlink", state="qa", branch="lot/member",
                         branch_target="origin/main", type="improvement", title="member")
        self.ops.get.return_value = self.item
        self.requirement = train_admissions.Requirement(1, self.base, self.head,
                                                       "sensitive", 2, self.base)
        self.policy = mock.Mock(return_value=self.requirement)
        self.preparation = train_candidates.Preparation(None, self.project,
            repository=self.repository, history=self.history, policy=self.policy)
        self.candidate = dict(work_item_id=1, branch="lot/member", target="main", causes=[],
            candidate=dict(sha=self.head, origin="frozen", declared=self.marks[0]))

    def prepare(self):
        return self.preparation.prepare(self.candidate, self.base)

    def test_real_published_git_and_history_need_no_queue_generation_or_individual_ci(self):
        before = copy.deepcopy(self.candidate)
        proof = self.prepare()
        self.assertEqual(proof["subject"]["frozen_sha"], self.head)
        self.assertEqual(set(proof["history"]["reviews"]["coverage"]), {self.first, self.head})
        self.assertNotIn("generation", proof["subject"])
        self.assertNotIn("admission", proof["history"])
        self.assertEqual([call[0] for call in self.ops.mock_calls], ["get", "milestones"])
        self.assertEqual(self.candidate, before)

    def test_abbreviated_supplier_sha_resolves_to_the_exact_published_commit(self):
        self.candidate["candidate"]["sha"] = self.head[:12]
        self.assertEqual(self.prepare()["subject"]["frozen_sha"], self.head)

    def test_moved_or_deleted_remote_branch_is_refused_despite_local_objects(self):
        for value in (self.first, None):
            refspec = (value + ":refs/heads/lot/member") if value else ":refs/heads/lot/member"
            self.git("push", "-q", "--force", "origin", refspec)
            with self.subTest(value=value), self.assertRaises(train.TrainError) as refused:
                self.prepare()
            self.assertIn("lot/member", str(refused.exception))
        self.policy.assert_not_called()

    def test_derived_candidate_and_provider_refusals_cannot_become_a_declared_freeze(self):
        self.candidate["candidate"]["origin"] = "verdict"
        with self.assertRaisesRegex(train.TrainError, "real declared freeze"):
            self.prepare()
        self.candidate["causes"] = ["blocked-verdict"]
        with self.assertRaisesRegex(train.TrainError, "blocked-verdict"):
            self.prepare()
        self.assertEqual(self.ops.mock_calls, [])

    def test_changed_qa_subject_and_current_base_refuse_before_historical_qualification(self):
        for field, value in (("state", "build"), ("app", "other"),
                             ("branch", "lot/other"), ("branch_target", "develop")):
            old = self.item[field]
            self.item[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(train.TrainError, "QA branch"):
                self.prepare()
            self.item[field] = old
        with self.assertRaisesRegex(train.TrainError, "current target base"):
            self.preparation.prepare(self.candidate, self.first)
        self.policy.assert_not_called()

    def test_new_same_sha_freeze_and_missing_author_or_review_still_refuse(self):
        self.marks.append(dict(self.marks[0], id=4, at_ts=9))
        with self.assertRaisesRegex(train.TrainError, "latest exact freeze"):
            self.prepare()
        self.marks.pop()
        receipt = self.receipts.pop("commit:" + self.first)
        with self.assertRaisesRegex(train.TrainError, "unknown historical author"):
            self.prepare()
        self.receipts["commit:" + self.first] = receipt
        self.marks[:] = [self.marks[0]]
        with self.assertRaisesRegex(train.TrainError, "missing independent"):
            self.prepare()

    def test_light_without_review_passes_but_explicit_blockage_is_preserved(self):
        self.marks[:] = [self.marks[0]]
        self.policy.return_value = dataclasses.replace(self.requirement, risk="light", reviews=0)
        self.assertEqual(self.prepare()["history"]["reviews"]["coverage"],
                         {self.first: [], self.head: []})
        self.marks.append(dict(id=4, kind="verdict", actor="blocked-reader", at_ts=9,
                               sha=self.head, verdict="blocked"))
        with self.assertRaisesRegex(train.TrainError, "unresolved blocking"):
            self.prepare()

    def test_release_or_wrong_policy_is_refused(self):
        self.item["type"] = "release"
        with self.assertRaisesRegex(train.TrainError, "release lots"):
            self.prepare()
        self.item["type"] = "improvement"
        self.policy.return_value = dataclasses.replace(self.requirement, head=self.first)
        with self.assertRaisesRegex(train.TrainError, "another lot, base or frozen head"):
            self.prepare()
