# SPDX-License-Identifier: AGPL-3.0-only
"""#7241: freeze answers old wakes; external wakes are not agent debt."""
import unittest
from .test_6423_debit_qa_merge_wake import H, T, event, frozen, item, kinds, run


class WakeDebtTest(unittest.TestCase):
    def report(self, freeze_at=None, wake_at=T - 5 * H, external=None):
        lot = item(7111, assignee=None if external else "author")
        lot["external_executor"] = external
        return run(items=[lot], milestones=(
            [frozen(7111, 1, freeze_at)] if freeze_at is not None else []),
            events=[event(7111, 1, wake_at,
                          "wake: reprise (due 2026-11-08 22:00 UTC)",
                          actor="author", state="build")])

    def test_freeze_after_due_preserves_wake_without_debt(self):
        report = self.report(freeze_at=T - 2 * H)
        self.assertNotIn((7111, "wake_overdue"), kinds(report))
        self.assertEqual(len(report["wakes"]), 1)
        self.assertTrue(report["wakes"][0]["overdue"])
        self.assertEqual(report["wakes"][0]["by"], "author")

    def test_freeze_before_due_does_not_answer_wake(self):
        self.assertIn((7111, "wake_overdue"),
                      kinds(self.report(freeze_at=T - 4 * H)))

    def test_rearmed_wake_after_freeze_still_counts(self):
        self.assertIn((7111, "wake_overdue"), kinds(self.report(
            freeze_at=T - 2 * H, wake_at=T - H)))

    def test_external_reservation_has_named_wait_without_wake_debt(self):
        for executor in ("bureau", "human:smichea"):
            with self.subTest(executor=executor):
                report = self.report(external=executor)
                self.assertNotIn((7111, "wake_overdue"), kinds(report))
                self.assertEqual(len(report["wakes"]), 1)
                self.assertEqual(report["external_waits"][0]["who"], executor)

    def test_ordinary_overdue_wake_still_counts(self):
        self.assertIn((7111, "wake_overdue"), kinds(self.report()))
