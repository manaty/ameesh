# SPDX-License-Identifier: AGPL-3.0-only
"""L81 (#289) — resident rotation: continuous measurement and thresholds.

No Postgres, no harness: the measurement is read from `TurnResult.usage`
(study §6.1) and the decision is a pure function (study §6.2). The rotation
and the native compaction themselves come next.
"""
from __future__ import annotations

import types
import unittest

from ameesh import resident_runner


def _result(**usage):
    """A finished turn's `TurnResult`, reduced to the usage it carries."""
    return types.SimpleNamespace(usage=usage)


class TurnRereadTest(unittest.TestCase):
    def test_sums_the_three_counters_of_a_call(self):
        self.assertEqual(resident_runner.turn_reread(
            {"input_tokens": 10, "cache_read_input_tokens": 1000,
             "cache_creation_input_tokens": 5, "output_tokens": 999}), 1015)

    def test_absent_or_none_reads_as_zero(self):
        self.assertEqual(resident_runner.turn_reread(None), 0)
        self.assertEqual(resident_runner.turn_reread({}), 0)
        self.assertEqual(resident_runner.turn_reread({"input_tokens": None}), 0)


class RereadMaxTest(unittest.TestCase):
    def test_default_when_absent_or_unreadable(self):
        for value in (None, "", "   ", "abc", "-5", "nan"):
            self.assertEqual(resident_runner.reread_max(value), 50_000_000.0,
                             value)

    def test_reads_tokens_and_zero_disables(self):
        self.assertEqual(resident_runner.reread_max("12000000"), 12_000_000.0)
        self.assertEqual(resident_runner.reread_max("0"), 0.0)


class MaintenanceTest(unittest.TestCase):
    def test_rotation_on_the_turn_ceiling(self):
        self.assertEqual(resident_runner.maintenance(
            context=1_000, window=200_000, reread=15_000_000,
            session_reread=15_000_000, cap=15_000_000), resident_runner.ROTATE)

    def test_rotation_on_the_session_cumulative_reread(self):
        self.assertEqual(resident_runner.maintenance(
            context=1_000, window=200_000, reread=1_000,
            session_reread=50_000_000, reread_max=50_000_000),
            resident_runner.ROTATE)

    def test_rotation_wins_over_compaction(self):
        self.assertEqual(resident_runner.maintenance(
            context=190_000, window=200_000, reread=15_000_000,
            session_reread=15_000_000, cap=15_000_000), resident_runner.ROTATE)

    def test_compaction_at_half_the_window(self):
        self.assertEqual(resident_runner.maintenance(
            context=100_000, window=200_000, reread=1_000,
            session_reread=1_000), resident_runner.COMPACT)
        self.assertEqual(resident_runner.maintenance(
            context=99_999, window=200_000, reread=1_000, session_reread=1_000), "")

    def test_no_window_no_compaction(self):
        self.assertEqual(resident_runner.maintenance(
            context=10 ** 9, window=None, reread=1_000, session_reread=1_000), "")
        self.assertEqual(resident_runner.maintenance(
            context=None, window=200_000, reread=1_000, session_reread=1_000), "")

    def test_disabled_thresholds(self):
        # cap and reread_max at 0 are disabled: only the soft threshold speaks
        self.assertEqual(resident_runner.maintenance(
            context=190_000, window=200_000, reread=10 ** 9,
            session_reread=10 ** 9, cap=0, reread_max=0), resident_runner.COMPACT)
        self.assertEqual(resident_runner.maintenance(
            context=1_000, window=200_000, reread=10 ** 9,
            session_reread=10 ** 9, cap=0, reread_max=0), "")

    def test_share_zero_disables_compaction(self):
        self.assertEqual(resident_runner.maintenance(
            context=199_999, window=200_000, reread=1_000, session_reread=1_000,
            share=0), "")


class ResidentMeasurementTest(unittest.TestCase):
    def _process(self) -> resident_runner.ResidentProcess:
        # measurement never touches the driver
        return resident_runner.ResidentProcess(object(), key=(), life_id="vie")

    def test_record_turn_accumulates(self):
        rp = self._process()
        rp.record_turn(_result(input_tokens=100, cache_read_input_tokens=900,
                               context_tokens=12_000, context_window=200_000))
        self.assertEqual((rp.last_context, rp.context_window), (12_000, 200_000))
        self.assertEqual((rp.last_reread, rp.session_reread), (1_000, 1_000))
        rp.record_turn(_result(input_tokens=50, cache_read_input_tokens=1_950,
                               context_tokens=40_000, context_window=200_000))
        self.assertEqual((rp.last_reread, rp.session_reread), (2_000, 3_000))
        self.assertEqual(rp.last_context, 40_000)

    def test_record_turn_without_usage_keeps_the_measure(self):
        rp = self._process()
        rp.record_turn(_result(context_tokens=12_000, context_window=200_000))
        rp.record_turn(_result())
        self.assertEqual((rp.last_context, rp.context_window), (12_000, 200_000))
        self.assertEqual((rp.last_reread, rp.session_reread), (0, 0))

    def test_maintenance_due_follows_the_last_turn(self):
        rp = self._process()
        self.assertEqual(rp.maintenance_due(), "")
        rp.record_turn(_result(context_tokens=150_000, context_window=200_000))
        self.assertEqual(rp.maintenance_due(), resident_runner.COMPACT)
        rp.record_turn(_result(input_tokens=15_000_000, context_tokens=150_000,
                               context_window=200_000))
        self.assertEqual(rp.maintenance_due(cap=15_000_000), resident_runner.ROTATE)

    def test_maintenance_due_reads_the_cumulative_reread(self):
        rp = self._process()
        rp.record_turn(_result(input_tokens=20_000_000, context_tokens=1_000,
                               context_window=200_000))
        self.assertEqual(rp.maintenance_due(reread_max=50_000_000), "")
        rp.record_turn(_result(input_tokens=31_000_000, context_tokens=1_000,
                               context_window=200_000))
        self.assertEqual(rp.maintenance_due(reread_max=50_000_000),
                         resident_runner.ROTATE)


if __name__ == "__main__":
    unittest.main()
