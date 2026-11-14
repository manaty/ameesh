# SPDX-License-Identifier: AGPL-3.0-only
"""A freeze succeeds only after its journal entry and replacement marks exist."""
import unittest
from unittest import mock

from ameesh import freeze_gate, review_authors, work
from .support import PgTestCase
from .test_3399_dangling_supersedes import _DanglingSupersededFreezeFixture


class RecordedFreezeTest(unittest.TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.row = {"id": 12, "kind": "frozen", "sha": self.sha,
                    "note": "ameesh-freeze-supersedes: 8"}

    def verify(self, rows):
        with mock.patch.object(freeze_gate, "milestones", return_value=rows):
            return freeze_gate.verify_recorded(
                None, 7353, self.row, self.sha, supersedes=[8])

    def test_recorded_replacement_is_confirmed(self):
        self.assertEqual(self.verify([self.row]), self.row)

    def test_success_without_write_is_refused(self):
        with self.assertRaisesRegex(work.WorkError, "not confirmed in journal"):
            self.verify([])

    def test_another_freeze_cannot_confirm_the_write(self):
        with self.assertRaises(work.WorkError):
            self.verify([dict(self.row, id=11)])

    def test_missing_replacement_mark_is_refused(self):
        with self.assertRaises(work.WorkError):
            self.verify([dict(self.row, note="checks green")])

    def test_wrong_head_is_refused(self):
        with self.assertRaises(work.WorkError):
            self.verify([dict(self.row, sha="b" * 40)])

    def test_unreadable_journal_cannot_report_success(self):
        with mock.patch.object(freeze_gate, "milestones", side_effect=OSError):
            with self.assertRaisesRegex(work.WorkError, "journal unreadable"):
                freeze_gate.verify_recorded(None, 7353, self.row, self.sha)


class ProvenanceCloneTest(unittest.TestCase):
    def test_plain_unknown_names_the_clone_read_by_the_gate(self):
        text = review_authors.unknown_refusal(["abc123"], repo="/scratch/gate")
        self.assertIn("repository read: /scratch/gate", text)

    def test_missing_freeze_ref_names_clone_and_preserves_remedy(self):
        unknown = review_authors.PriorFreezeUnknown(
            "abc123", freeze_id=8, freeze_sha="a" * 40, lot=7266,
            reachable=False, repo="/scratch/gate")
        text = review_authors.unknown_refusal([unknown], head_sha="b" * 40)
        self.assertIn("repository read: /scratch/gate", text)
        self.assertIn("--supersedes 8", text)
        self.assertIn("prior freeze #8", text)


class ReplacementCliTest(_DanglingSupersededFreezeFixture):
    def test_cli_persists_replacement_of_freeze_without_any_ref(self):
        proc = self.mesh(
            "work", "milestone", str(self.lot), "frozen", "--sha", self.head_b,
            "--repo", self.repo.root, "--actor", "impl",
            "--supersedes", str(self.op_freeze["id"]), env=PgTestCase.env.__func__(type(self), AGENT_MAIL_NAME="impl"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("milestone frozen added", proc.stdout)
        latest = work.milestones(self.db, self.lot)[0]
        self.assertEqual(latest["sha"], self.head_b)
        self.assertIn("ameesh-freeze-supersedes: %d" % self.op_freeze["id"],
                      latest["note"])
        self.assertEqual(self.read(latest)[1], [])
