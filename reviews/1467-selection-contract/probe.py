# SPDX-License-Identifier: AGPL-3.0-only
"""#1756 candidates at the #1467 history boundary; Git real, storage fixtures.

Loads the exact supplier object without merging it or installing a provider.
Only consumer scenarios below run, not the supplier's suite.
"""
import argparse
import copy
import re
import subprocess
import types
import unittest

from ameesh import train
from tests.test_1467_train_history import RecordedHistoryTest


class SelectionBoundary(RecordedHistoryTest):
    candidate = None

    def test_stale_declared_freeze_is_diagnostic_not_queue_rewrite(self):
        self.marks[0]["sha"] = self.first
        before = copy.deepcopy((self.entry, self.marks))
        candidate = self.candidate(self.marks)
        self.assertEqual((candidate["origin"], candidate["sha"]), ("verdict", self.head))
        self.assertIn("declared-freeze-stale", candidate["notes"])
        with self.assertRaisesRegex(train.TrainError, "latest exact freeze"):
            self.qualify()
        self.assertEqual((self.entry, self.marks), before)
        self.assertEqual([call[0] for call in self.ops.mock_calls], ["milestones"])

    def test_missing_declared_freeze_is_visible_without_inventing_a_generation(self):
        self.marks = [mark for mark in self.marks if mark["kind"] != "frozen"]
        before = copy.deepcopy((self.entry, self.marks))
        candidate = self.candidate(self.marks)
        self.assertEqual((candidate["origin"], candidate["sha"]), ("verdict", self.head))
        with self.assertRaisesRegex(train.TrainError, "current freeze missing"):
            self.qualify()
        self.assertEqual((self.entry, self.marks), before)

    def test_light_candidate_needs_no_verdict_but_preserves_exact_author_history(self):
        self.marks = [mark for mark in self.marks if mark["kind"] == "frozen"]
        candidate = self.candidate(self.marks)
        self.assertEqual((candidate["origin"], candidate["sha"]), ("frozen", self.head))
        self.assertIn("no-verdict", candidate["notes"])
        proof = self.history.qualify(self.entry, self.base, required=0)
        self.assertEqual(proof["reviews"]["coverage"], {self.first: [], self.head: []})
        with self.assertRaisesRegex(train.TrainError, "missing independent"):
            self.qualify()
        del self.receipts["commit:" + self.first]
        with self.assertRaisesRegex(train.TrainError, "unknown historical author"):
            self.history.qualify(self.entry, self.base, required=0)

    def test_new_freeze_after_prior_verdict_keeps_a_named_refusal(self):
        self.marks.append(dict(id=4, kind="frozen", actor="writer", sha=self.first, at_ts=9))
        candidate = self.candidate(self.marks)
        self.assertEqual(candidate["sha"], "")
        self.assertIn("freeze-after-verdict", candidate["causes"])
        with self.assertRaisesRegex(train.TrainError, "latest exact freeze"):
            self.qualify()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supplier", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args.commit):
        parser.error("exact full supplier commit required")
    resolved = subprocess.check_output([
        "git", "-C", args.supplier, "rev-parse", "--verify", args.commit + "^{commit}"
    ], text=True).strip()
    if resolved != args.commit:
        parser.error("supplier must name the exact commit object")
    path = "src/ameesh/plan_git.py"
    source = subprocess.check_output([
        "git", "-C", args.supplier, "show", args.commit + ":" + path
    ], text=True)
    provider = types.ModuleType("ameesh._selection_probe_1756")
    exec(compile(source, args.commit + ":" + path, "exec"), provider.__dict__)
    SelectionBoundary.candidate = staticmethod(provider.freeze_candidate)
    names = (
        "test_stale_declared_freeze_is_diagnostic_not_queue_rewrite",
        "test_missing_declared_freeze_is_visible_without_inventing_a_generation",
        "test_light_candidate_needs_no_verdict_but_preserves_exact_author_history",
        "test_new_freeze_after_prior_verdict_keeps_a_named_refusal",
    )
    suite = unittest.TestSuite(SelectionBoundary(name) for name in names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
