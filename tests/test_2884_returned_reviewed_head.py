# SPDX-License-Identifier: AGPL-3.0-only
"""#2884 : un regel peut revenir à une tête dont les avis sont déjà acquis."""
import unittest

from ameesh import merge_queue, plan_git


A = "eb919bc" + "a" * 33
B = "ae252cbf" + "b" * 32


def mark(kind, sha, at, mid, actor="", verdict=""):
    return dict(kind=kind, sha=sha, at_ts=at, id=mid, actor=actor, verdict=verdict)


def timeline():
    # Frise du lot #2845 : avis sur la source, tête composée relue,
    # puis retour explicite à la source.
    return [
        mark("verdict", A, 10, 14646, "auditeur", "ok"),
        mark("verdict", A, 11, 14650, "impl-01", "ok"),
        mark("verdict", A, 12, 14651, "dev3", "ok"),
        mark("frozen", B, 20, 14713),
        mark("verdict", B, 21, 14714, "auditeur", "ok"),
        mark("frozen", A[:12], 30, 14727),
    ]


class ReturnedReviewedHeadTest(unittest.TestCase):
    def test_return_to_reviewed_source(self):
        for rows in (timeline(), list(reversed(timeline()))):
            with self.subTest(order=[r["id"] for r in rows]):
                out = plan_git.freeze_candidate(rows)
                self.assertEqual(out["sha"], A)
                self.assertEqual(out["origin"], "frozen")
                self.assertEqual(out["causes"], [])
                self.assertIn("returned-to-reviewed-head", out["notes"])
                self.assertEqual(out["declared"]["id"], 14727)
                self.assertEqual(out["verdict"]["id"], 14651)

    def test_current_head_block_cannot_be_hidden(self):
        for at in (13, 31):
            with self.subTest(block_at=at):
                rows = timeline() + [mark("verdict", A, at, 14728, "impl-01", "blocked")]
                out = plan_git.freeze_candidate(rows)
                self.assertEqual(out["sha"], "")
                self.assertEqual(out["causes"], ["blocked-verdict"])

    def test_only_same_reviewer_can_lift_current_head_block(self):
        for actor, allowed in (("impl-01", True), ("dev3", False)):
            with self.subTest(actor=actor):
                rows = timeline() + [
                    mark("verdict", A, 13, 14652, "impl-01", "blocked"),
                    mark("verdict", A, 14, 14653, actor, "ok"),
                ]
                out = plan_git.freeze_candidate(rows)
                self.assertEqual(bool(out["sha"]), allowed)
                self.assertEqual(out["causes"], [] if allowed else ["blocked-verdict"])

    def test_block_on_intermediate_head_does_not_cancel_source_reviews(self):
        rows = timeline()
        rows[-2]["verdict"] = "blocked"
        out = plan_git.freeze_candidate(rows)
        self.assertEqual(out["sha"], A)
        self.assertEqual(out["causes"], [])

    def test_no_matching_ok_still_requires_review(self):
        rows = [r for r in timeline() if r["kind"] != "verdict" or r["sha"] != A]
        out = plan_git.freeze_candidate(rows)
        self.assertEqual(out["sha"], "")
        self.assertEqual(out["causes"], ["freeze-after-verdict"])

    def test_timestamp_only_block_after_return_still_applies(self):
        out = plan_git.freeze_candidate(timeline() + [
            mark("verdict", "", 31, 14728, "impl-01", "blocked")])
        self.assertEqual(out["sha"], "")
        self.assertEqual(out["causes"], ["blocked-verdict"])

    def test_candidate_agrees_with_two_family_review_gate(self):
        families = {"author": "codex", "auditeur": "claude",
                    "impl-01": "deepseek", "dev3": "deepseek"}
        rows = timeline()
        candidate = plan_git.freeze_candidate(rows)
        gate = merge_queue.review_gate(
            None, rows, A, item={"assignee": "author"}, required=2,
            klass={"class": "sensitive"}, family_of=families.get)
        self.assertTrue(gate["ok"], gate)
        self.assertEqual(candidate["sha"], A)
        self.assertEqual(candidate["causes"], [])

    def test_future_review_of_new_head_still_supersedes_freeze(self):
        other = "c" * 40
        out = plan_git.freeze_candidate(timeline() + [
            mark("verdict", other, 31, 14728, "dev4", "ok")])
        self.assertEqual(out["sha"], other)
        self.assertEqual(out["origin"], "verdict")
        self.assertNotIn("returned-to-reviewed-head", out["notes"])


if __name__ == "__main__":
    unittest.main()
