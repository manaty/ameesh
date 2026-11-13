# SPDX-License-Identifier: AGPL-3.0-only
"""#7249: automatic roadmap sleep is stable capacity, not lost work time."""
import copy
import unittest
from .test_3256_debit_capacity import H, T, agent, measure, split_lot


class StableCapacityTest(unittest.TestCase):
    def test_sleep_transition_and_repeated_reads_have_same_capacity(self):
        awake = agent("worker")
        asleep = dict(awake, operator_paused=True,
                      operator_pause_reason="roadmap: épuisée ameesh (#3256 §2.3)")
        original = copy.deepcopy(asleep)
        args = dict(roadmap_notes=[{
            "work_item_id": 90, "note": "roadmap: épuisée ameesh", "at_ts": T - H}],
            open_lots=[split_lot()])
        expected = measure([awake], **args)
        for _ in range(2):
            actual = measure([asleep], **args)
            self.assertEqual(actual, expected)
            self.assertEqual(actual["mesh"]["wakeable"], 1)
            self.assertEqual(actual["mesh"]["idle_agent_hours"], 0)
        self.assertEqual(asleep, original)

    def test_new_lot_counts_only_time_since_creation(self):
        args = dict(roadmap_notes=[{
            "work_item_id": 90, "note": "roadmap: épuisée ameesh", "at_ts": T - H}],
            open_lots=[split_lot()],
            startable=[{"id": 12, "app": "ameesh", "created_ts": T - H / 2}])
        first = measure([agent("worker")], **args)
        self.assertEqual(first, measure([agent("worker")], **args))
        self.assertEqual(first["mesh"]["idle_agent_hours"], 0.5)
        self.assertEqual(first["projects"][0]["roadmap_exhausted"]["lot"], 90)

    def test_human_pause_budget_pause_and_dead_runner_still_excluded(self):
        for row in (agent("worker", operator_paused=True, operator_pause_reason="maintenance"),
                    agent("worker", status="blocked"),
                    agent("worker", lease_live=False)):
            with self.subTest(row=row):
                self.assertEqual(measure([row])["mesh"]["wakeable"], 0)
