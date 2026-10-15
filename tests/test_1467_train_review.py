# SPDX-License-Identifier: AGPL-3.0-only
"""Historical subject identity and per-author independence, without a registry."""
import dataclasses
import unittest

from ameesh.train import TrainError
from ameesh.train_review import MARK_FIELDS, Receipt, qualify

HEAD, FIRST = "a" * 40, "b" * 40


def author(commit=FIRST, actor="author", harness="codex"):
    subject = "commit:" + commit
    return Receipt(subject, commit, actor, actor + "-session", actor + "-turn",
                   harness, "exact-commit-event:" + commit)


def verdict(number, actor, harness, *, commit=HEAD, status="ok"):
    mark = dict(id=number, kind="verdict", actor=actor, at_ts=float(number),
                sha=commit, verdict=status)
    proof = Receipt("milestone:%d" % number, commit, actor, actor + "-session",
                    actor + "-turn", harness, "exact-milestone-event:%d" % number,
                    {key: mark[key] for key in MARK_FIELDS})
    return mark, proof


class HistoricalReviewsTest(unittest.TestCase):
    def setUp(self):
        self.commits = [FIRST, HEAD]
        authors = [author(commit) for commit in self.commits]
        self.marks, self.receipts = [], {a.subject: a for a in authors}
        self.add(1, "reviewer-one", "claude")
        self.add(2, "reviewer-two", "deepseek")

    def add(self, number, actor, harness, **kw):
        mark, proof = verdict(number, actor, harness, **kw)
        self.marks.append(mark)
        self.receipts[proof.subject] = proof

    def run_gate(self, *, required=2):
        return qualify(HEAD, self.commits, self.marks, self.receipts, required=required)

    def test_every_commit_is_covered_by_two_historical_families(self):
        proof = self.run_gate()
        self.assertEqual(set(proof["coverage"]), set(self.commits))
        self.assertTrue(all(len(votes) == 2 for votes in proof["coverage"].values()))

    def test_correction_is_attributed_to_its_actual_author_and_needs_coverage(self):
        self.receipts["commit:" + HEAD] = author(HEAD, "corrector", "claude")
        with self.assertRaisesRegex(TrainError, "missing independent.*corrector"):
            self.run_gate()
        # The original author can review the correction, while their own
        # original work still needs Claude + DeepSeek on the new final head.
        self.add(3, "author", "codex")
        proof = self.run_gate()
        self.assertEqual(proof["coverage"][FIRST], ["milestone:1", "milestone:2"])
        self.assertEqual(set(proof["coverage"][HEAD]), {"milestone:2", "milestone:3"})

    def test_unknown_commit_or_reviewer_is_never_supplied_by_current_identity(self):
        self.receipts["commit:" + FIRST] = None
        with self.assertRaisesRegex(TrainError, "unknown historical author"):
            self.run_gate()
        self.receipts["commit:" + FIRST] = author()
        self.receipts["milestone:1"] = None
        with self.assertRaisesRegex(TrainError, "missing independent"):
            self.run_gate()

    def test_missing_session_turn_or_unknown_harness_cannot_vote(self):
        original = self.receipts["milestone:1"]
        for field, value in (("session", ""), ("turn", ""), ("harness", "unknown")):
            with self.subTest(field=field):
                self.receipts["milestone:1"] = dataclasses.replace(original, **{field: value})
                with self.assertRaisesRegex(TrainError, "missing independent"):
                    self.run_gate()

    def test_another_actor_ok_cannot_clear_a_blocking_verdict(self):
        self.add(3, "blocked-reviewer", "claude", status="blocked")
        self.add(4, "another-reviewer", "claude")
        with self.assertRaisesRegex(TrainError, "unresolved blocking.*blocked-reviewer"):
            self.run_gate()
        self.add(5, "blocked-reviewer", "claude")
        self.run_gate()

    def test_refreeze_does_not_erase_blockage_without_final_head_review(self):
        self.add(3, "reviewer-one", "claude", commit=FIRST, status="blocked")
        self.add(4, "reviewer-one", "claude", commit=FIRST)
        with self.assertRaisesRegex(TrainError, "unresolved blocking"):
            self.run_gate()
        self.add(5, "reviewer-one", "claude")
        self.run_gate()

    def test_verdict_cannot_be_rebound_to_another_actor_date_head_or_id(self):
        original = self.receipts["milestone:1"]
        for key, value in (("actor", "author"), ("at_ts", 900), ("sha", FIRST), ("id", 8)):
            with self.subTest(key=key):
                fields = dict(original.milestone, **{key: value})
                self.receipts["milestone:1"] = dataclasses.replace(original, milestone=fields)
                with self.assertRaisesRegex(TrainError, "different facts"):
                    self.run_gate()

    def test_same_actor_or_session_and_duplicate_family_do_not_supply_independence(self):
        original = self.receipts["milestone:1"]
        for change in (dict(actor="author"), dict(session="author-session"),
                       dict(session="reviewer-two-session"), dict(harness="deepseek")):
            with self.subTest(change=change):
                altered = dataclasses.replace(original, **change)
                self.receipts["milestone:1"] = altered
                with self.assertRaises(TrainError):
                    self.run_gate()

    def test_latest_verdict_on_other_head_cannot_vouch_for_current_head(self):
        self.add(3, "reviewer-one", "claude", commit=FIRST)
        with self.assertRaisesRegex(TrainError, "missing independent"):
            self.run_gate()

    def test_valid_pair_is_found_when_first_pair_shares_a_session(self):
        self.receipts["milestone:1"] = dataclasses.replace(
            self.receipts["milestone:1"], session="reviewer-two-session")
        self.add(3, "reviewer-one-b", "claude")
        proof = self.run_gate()
        self.assertEqual(set(proof["coverage"][FIRST]), {"milestone:2", "milestone:3"})

    def test_light_policy_still_preserves_blocking_verdicts(self):
        self.add(3, "reviewer-one", "claude", status="blocked")
        with self.assertRaisesRegex(TrainError, "unresolved blocking"):
            self.run_gate(required=0)
