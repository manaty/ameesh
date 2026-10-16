# SPDX-License-Identifier: AGPL-3.0-only
"""Historical subject identity and per-author independence, without a registry."""
import dataclasses
import types
import unittest
from unittest import mock

from ameesh.train import TrainError
from ameesh.train_review import MARK_FIELDS, Receipt, qualify, qualify_recorded

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


class RecordedReviewsTest(HistoricalReviewsTest):
    """The supplier boundary returns dictionaries, not consumer dataclasses."""

    def setUp(self):
        super().setUp()
        self.db = object()

        class ProvenanceError(Exception):
            pass

        def receipt(subject):
            value = self.receipts.get(subject)
            return dataclasses.asdict(value) if value is not None else None

        self.provider = types.SimpleNamespace(
            ProvenanceError=ProvenanceError,
            receipt_for_commit=mock.Mock(side_effect=lambda db, sha: receipt("commit:" + sha)),
            receipt_for_milestone=mock.Mock(
                side_effect=lambda db, number: receipt("milestone:%s" % number)))

    def run_gate(self, *, required=2):
        return qualify_recorded(self.db, HEAD, self.commits, self.marks,
                                required=required, provider=self.provider)

    def test_compiled_family_declaration_and_independence_reach_recorded_validator(self):
        def check(**rules):
            return qualify_recorded(self.db, HEAD, self.commits, self.marks,
                                    required=2, provider=self.provider, **rules)
        with self.assertRaisesRegex(TrainError, "missing independent"):
            check(families=("codex", "claude"))
        self.add(3, "reviewer-three", "claude")
        proof = check(families=("codex", "claude"), distinct_families=False)
        self.assertEqual(proof["coverage"][FIRST], ["milestone:1", "milestone:3"])
        self.add(4, "same-author-family", "codex")
        with self.assertRaisesRegex(TrainError, "missing independent"):
            check(families=("codex",))
        # A declared family change still never permits the author's actor
        # or session to approve their own content.
        for sha in self.commits:
            self.receipts["commit:" + sha] = author(sha, "author", "claude")
        proof = check(families=("codex", "claude"), exclude_author_family=False)
        self.assertEqual(len(proof["coverage"][FIRST]), 2)

    def test_abbreviated_mark_keeps_original_facts_and_complete_commit(self):
        self.marks[0]["sha"] = HEAD[:12]
        receipt = self.receipts["milestone:1"]
        self.receipts[receipt.subject] = dataclasses.replace(
            receipt, milestone=dict(receipt.milestone, sha=HEAD[:12]))
        result = self.run_gate()
        self.assertEqual(result["reviews"][0]["commit"], HEAD)
        self.assertEqual(result["reviews"][0]["milestone"]["sha"], HEAD[:12])
        self.provider.receipt_for_commit.assert_any_call(self.db, FIRST)
        self.provider.receipt_for_milestone.assert_any_call(self.db, 1)

    def test_supplier_corruption_becomes_explicit_train_refusal(self):
        for reader in ("receipt_for_commit", "receipt_for_milestone"):
            with self.subTest(reader=reader), mock.patch.object(
                    self.provider, reader,
                    mock.Mock(side_effect=self.provider.ProvenanceError("different fact"))):
                with self.assertRaisesRegex(TrainError, "contradictory.*different fact"):
                    self.run_gate()

    def test_missing_or_extra_receipt_field_is_refused(self):
        valid = dataclasses.asdict(author())
        for invalid in ({key: value for key, value in valid.items() if key != "turn"},
                        dict(valid, guessed=True)):
            with self.subTest(record=invalid), mock.patch.object(
                    self.provider, "receipt_for_commit", lambda *args: invalid):
                with self.assertRaisesRegex(TrainError, "malformed historical"):
                    self.run_gate()

    def test_supplier_cannot_substitute_another_subject(self):
        with mock.patch.object(self.provider, "receipt_for_commit",
                               lambda *args: dataclasses.asdict(author(HEAD))):
            with self.assertRaisesRegex(TrainError, "another subject"):
                self.run_gate()

    def test_no_provider_cannot_fall_back_to_current_registry(self):
        with self.assertRaisesRegex(TrainError, "trusted exact provenance provider"):
            qualify_recorded(self.db, HEAD, self.commits, self.marks,
                             required=0, provider=None)
