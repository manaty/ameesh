# SPDX-License-Identifier: AGPL-3.0-only
"""Candidate inventory does not grant admission or create queue generations."""
import copy
import types
import unittest
from unittest import mock

from ameesh import storage, train, train_candidates
from .support import PgTestCase


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
