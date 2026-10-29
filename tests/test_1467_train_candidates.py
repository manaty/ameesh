# SPDX-License-Identifier: AGPL-3.0-only
"""Candidate inventory does not grant admission or create queue generations."""
import copy
import dataclasses
import os
import subprocess
import types
import unittest
from unittest import mock

from ameesh import (resolutions, storage, train, train_admissions, train_candidates,
                    train_git, work)
from .support import PgTestCase
from . import test_1467_train_history as history_tests


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.project = types.SimpleNamespace(name="nexlink", repo="/unused", target="develop")
        self.ops = mock.Mock()
        self.item = dict(id=2005, app="nexlink", state="qa", branch="agent/member",
                         branch_target="origin/develop", type="improvement", title="member")
        self.ops.items.return_value = [self.item]
        self.ops.items.side_effect = lambda **kw: [
            row for row in self.ops.items.return_value if row["state"] == kw["state"]]
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
        self.assertEqual([call[0] for call in self.ops.mock_calls], ["items", "milestones", "items"])
        self.assertEqual(self.ops.items.call_args_list, [
            mock.call(state="qa", assignee=None, limit=None),
            mock.call(state="validated", assignee=None, limit=None)])
        self.ops.milestones.assert_called_once_with(2005, None)
        self.reader.assert_called_once_with(self.marks, waived=False)
        self.assertEqual((self.item, self.marks, self.result), before)
        self.assertNotIn("generation", result["candidates"][0])

    def test_pilot_waiver_is_read_from_the_row_and_passed_to_the_candidate(self):
        self.item["reviewer"] = work.REVIEWER_NONE
        self.inventory.read()
        self.reader.assert_called_once_with(self.marks, waived=True)

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


class PreparationTest(history_tests.HistoryFixture, PgTestCase):
    def setUp(self):
        super().setUp()
        self.git("branch", "main", self.base)
        self.git("branch", "lot/member", self.head)
        origin = os.path.join(self.repo, "origin.git")
        subprocess.run(["git", "clone", "-q", "--bare", self.repo, origin],
                       check=True, capture_output=True)
        self.git("remote", "add", "origin", origin)
        self.project = types.SimpleNamespace(name="nexlink", repo=self.repo, target="main")
        self.db.execute("UPDATE work_items SET app = %s, branch = %s,"
                        " branch_target = %s WHERE id = %s",
                        ("nexlink", "lot/member", "origin/main", self.lot))
        self.item = work.get(self.db, self.lot)
        clone = resolutions.Replayer(self.repo, os.path.join(self.repo, "private.clone"))
        self.repository = train_git.Repository(self.project, clone)
        self.history.path = clone.path
        self.requirement = train_admissions.Requirement(
            self.lot, self.base, self.head, "sensitive", 2, self.base,
            files=({"path": "content", "class": "sensitive", "rule": None},))
        self.policy = mock.Mock(return_value=self.requirement)
        self.preparation = train_candidates.Preparation(
            self.db, self.project, repository=self.repository,
            history=self.history, policy=self.policy)
        self.candidate = dict(work_item_id=self.lot, branch="lot/member",
            target="main", causes=[],
            candidate=dict(sha=self.head, origin="frozen", declared=self.frozen))

    def prepare(self):
        return self.preparation.prepare(self.candidate, self.base)

    def last_freeze(self):
        return storage.of(self.db).work.milestones(self.lot, None)[0]

    def test_real_published_git_and_history_need_no_queue_generation_or_individual_ci(self):
        before = copy.deepcopy(self.candidate)
        proof = self.prepare()
        self.assertEqual(proof["subject"]["frozen_sha"], self.head)
        self.assertTrue(proof["history"]["reviews"]["gate"]["ok"])
        # #2965: the shared admissibility instant is published with the
        # preparation proof, so the shadow can apply the active late filter.
        self.assertIsInstance(proof["history"]["reviews"]["admitted_ts"], float)
        self.assertNotIn("generation", proof["subject"])
        self.assertNotIn("admission", proof["history"])
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

    def test_changed_qa_subject_and_current_base_refuse_before_historical_qualification(self):
        for field, value in (("state", "build"), ("app", "other"),
                             ("branch", "lot/other"), ("branch_target", "develop")):
            original = work.get(self.db, self.lot)[field]
            self.db.execute("UPDATE work_items SET " + field + " = %s WHERE id = %s",
                            (value, self.lot))
            with self.subTest(field=field), self.assertRaisesRegex(train.TrainError, "QA branch"):
                self.prepare()
            self.db.execute("UPDATE work_items SET " + field + " = %s WHERE id = %s",
                            (original, self.lot))
        with self.assertRaisesRegex(train.TrainError, "current target base"):
            self.preparation.prepare(self.candidate, self.first)
        self.policy.assert_not_called()

    def test_new_same_sha_freeze_and_missing_author_or_review_still_refuse(self):
        self.ops.add_milestone(self.lot, "frozen", sha=self.head, actor="author",
                               verdict=None, note="same head, new generation")
        with self.assertRaisesRegex(train.TrainError, "latest exact freeze"):
            self.prepare()
        # the preparation reads the latest freeze; remove the extra one
        self.db.execute("DELETE FROM work_item_milestones WHERE work_item_id = %s"
                        " AND kind = 'frozen' AND id <> %s",
                        (self.lot, self.frozen["id"]))
        # #2808 : le titulaire n'est auteur que par un commit de la plage.
        # Sans reçu **ni** tour pour la famille, ses deux commits (first et
        # head) restent sans famille prouvée : retirer les deux reçus, sinon
        # celui de `head` suffit encore à prouver la famille de l'auteur.
        self.db.execute("DELETE FROM provenance_receipts WHERE subject IN (%s, %s)",
                        ("commit:" + self.first, "commit:" + self.head))
        self.db.execute("DELETE FROM turn_costs WHERE agent = 'author'")
        with self.assertRaisesRegex(train.TrainError,
                                    "unknown (model family|historical author)|no known model family"):
            self.prepare()
        self.receipt_commit(self.first, "author", "deepseek")
        self.turn("author", "deepseek")
        self.db.execute("DELETE FROM work_item_milestones WHERE work_item_id = %s"
                        " AND kind = 'verdict'", (self.lot,))
        with self.assertRaisesRegex(train.TrainError, "no review verdict ok"):
            self.prepare()

    def test_light_without_review_passes_but_explicit_blockage_is_preserved(self):
        self.policy.return_value = dataclasses.replace(
            self.requirement, risk="light", reviews=0,
            files=({"path": "content", "class": "light", "rule": None},))
        self.assertTrue(self.prepare()["history"]["reviews"]["gate"]["ok"])
        self.ops.add_milestone(self.lot, "verdict", sha=self.head,
                               actor="blocked-reader", verdict="blocked", note="")
        with self.assertRaisesRegex(train.TrainError, "blocked verdict"):
            self.prepare()

    def test_release_or_wrong_policy_is_refused(self):
        self.project.target = "release/1.0"
        self.candidate["target"] = "release/1.0"
        self.db.execute("UPDATE work_items SET branch_target = 'release/1.0'"
                        " WHERE id = %s", (self.lot,))
        with self.assertRaisesRegex(train.TrainError, "release lots"):
            self.prepare()
        self.project.target = "main"
        self.candidate["target"] = "main"
        self.db.execute("UPDATE work_items SET branch_target = 'origin/main'"
                        " WHERE id = %s", (self.lot,))
        self.policy.return_value = dataclasses.replace(self.requirement, head=self.first)
        with self.assertRaisesRegex(train.TrainError, "another lot, base or frozen head"):
            self.prepare()
