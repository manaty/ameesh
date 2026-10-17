# SPDX-License-Identifier: AGPL-3.0-only
"""Real Git ranges, complete milestones and exact provenance at admission."""
import dataclasses
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import storage, train, train_history, work
from .support import PgTestCase
from .test_1467_train import row
from .test_1467_train_review import author, verdict


class RecordedHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name
        # PreparationTest creates main at base; never inherit the host's
        # init.defaultBranch (main on the CI runners).
        self.git("init", "-q", "-b", "fixture")
        self.git("config", "user.name", "fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.base = self.commit("base")
        self.first = self.commit("author")
        self.head = self.commit("correction")
        self.entry = dict(row(1), frozen_sha=self.head, frozen_id=1)
        self.marks = [dict(id=1, kind="frozen", actor="writer", sha=self.head, at_ts=1)]
        self.receipts = {"commit:" + sha: dataclasses.asdict(author(sha))
                         for sha in (self.first, self.head)}
        for number, actor, family in ((2, "r1", "claude"), (3, "r2", "deepseek")):
            mark, receipt = verdict(number, actor, family, commit=self.head)
            self.marks.append(mark)
            self.receipts[receipt.subject] = dataclasses.asdict(receipt)
        self.ops = mock.Mock()
        self.ops.milestones.side_effect = lambda lot, limit: list(reversed(self.marks))
        patch = mock.patch.object(storage, "of", return_value=types.SimpleNamespace(work=self.ops))
        patch.start()
        self.addCleanup(patch.stop)
        self.provider = types.SimpleNamespace(ProvenanceError=RuntimeError,
            receipt_for_commit=lambda db, sha: self.receipts.get("commit:" + sha),
            receipt_for_milestone=lambda db, mid: self.receipts.get("milestone:%s" % mid))
        self.history = train_history.Recorded(None, self.path, provider=self.provider)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", self.path, *args],
                                       text=True, stderr=subprocess.PIPE).strip()

    def commit(self, content):
        with open(os.path.join(self.path, "content"), "w") as fh:
            fh.write(content)
        self.git("add", "content")
        self.git("commit", "-qm", content)
        return self.git("rev-parse", "HEAD")

    def qualify(self):
        return self.history.qualify(self.entry, self.base, required=2)

    def test_every_commit_in_exact_range_is_covered_without_checkout_dependency(self):
        self.git("checkout", "-q", "--detach", self.base)
        proof = self.qualify()
        self.assertEqual(set(proof["reviews"]["coverage"]), {self.first, self.head})
        self.ops.milestones.assert_called_once_with(1, None)

    def test_old_blockage_is_not_hidden_by_more_than_one_hundred_later_marks(self):
        self.marks.insert(0, dict(id=4, kind="verdict", actor="blocked-reader", at_ts=0,
                                 sha=self.head, verdict="blocked"))
        self.marks.extend(dict(id=n, kind="frozen", actor="writer", at_ts=n,
                               sha=self.head) for n in range(5, 110))
        self.entry["frozen_id"] = 109
        with self.assertRaisesRegex(train.TrainError, "unresolved blocking.*blocked-reader"):
            self.qualify()

    def test_new_freeze_same_head_invalidates_old_queue_generation(self):
        self.marks.append(dict(id=5, kind="frozen", actor="writer", at_ts=5, sha=self.head))
        with self.assertRaisesRegex(train.TrainError, "latest exact freeze"):
            self.qualify()

    def test_abbreviated_freeze_is_resolved_but_another_commit_is_refused(self):
        self.marks[0]["sha"] = self.head[:10]
        self.qualify()
        self.marks[0]["sha"] = self.first
        with self.assertRaisesRegex(train.TrainError, "latest exact freeze"):
            self.qualify()

    def test_missing_author_is_not_filled_from_git_committer_or_other_receipts(self):
        del self.receipts["commit:" + self.first]
        with self.assertRaisesRegex(train.TrainError, "unknown historical author"):
            self.qualify()

    def test_shallow_history_is_refused_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as clone:
            subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + self.path,
                            clone], check=True, capture_output=True)
            history = train_history.Recorded(None, clone, provider=self.provider)
            with self.assertRaisesRegex(train.TrainError, "shallow clone refused"):
                history.qualify(self.entry, self.base, required=2)

    def test_already_contained_head_and_unknown_base_supply_no_admission(self):
        for base in (self.head, "0" * 40):
            with self.subTest(base=base), self.assertRaises(train.TrainError):
                self.history.qualify(self.entry, base, required=2)


class CompleteMilestonesTest(PgTestCase):
    def test_explicit_unbounded_read_preserves_blockage_beyond_default_page(self):
        item = work.add(self.db, title="train history", source="fixture", actor="fixture")
        ops = storage.of(self.db).work
        before = len(ops.milestones(item["id"], None))
        self.db.execute("INSERT INTO work_item_milestones(work_item_id,kind,sha,actor,verdict)"
                        " SELECT %s, 'verdict', %s, 'reader',"
                        " CASE WHEN n = 1 THEN 'blocked' ELSE 'ok' END"
                        " FROM generate_series(1, 110) n", (item["id"], "a" * 40))
        self.assertEqual(len(ops.milestones(item["id"], 100)), 100)
        marks = ops.milestones(item["id"], None)
        self.assertEqual(len(marks), before + 110)
        self.assertEqual(sum(m["verdict"] == "blocked" for m in marks), 1)
