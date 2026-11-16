# SPDX-License-Identifier: AGPL-3.0-only
"""A quantized step needs confirmation before projecting a reset."""
import unittest

from ameesh import cost


class ResumeSeriesTest(unittest.TestCase):
    def setUp(self):
        self.start = 1_800_000_000.0
        self.hour = 3600.0
        self.week = 7 * 24 * self.hour
        self.reset = self.start + 0.4 * self.week

    def announcement(self, series):
        now = self.start + series[-1][0] * self.hour
        gauge = cost.Gauge('claude', 'seven_day', series[-1][1], self.reset, self.week)
        history = [dict(harness='claude', key='seven_day', account=None,
                        used=used, resets_at_ts=self.reset,
                        produced_ts=self.start + hours * self.hour)
                   for hours, used in series]
        rate = cost.consumption_rate(history, gauge, now=now, reserve=0.1)
        return gauge.free_at(now, 0.1, rate=rate), rate, gauge.exceeded(now, 0.1)

    def test_single_step_and_plateau_do_not_announce_reset(self):
        series = [(0, 0.67)]
        for point in [(0.5, 0.68), (1, 0.68), (2, 0.68), (4, 0.68), (6, 0.68)]:
            series.append(point)
            with self.subTest(point=point):
                until, rate, held = self.announcement(series)
                self.assertLess(until, self.reset)
                self.assertEqual(rate, 0)
                self.assertTrue(held)

    def test_second_rise_confirms_sustained_rate(self):
        until, rate, held = self.announcement([(0, 0.67), (0.5, 0.68), (1, 0.69)])
        self.assertGreater(rate, 0)
        self.assertEqual(until, self.reset)
        self.assertTrue(held)

    def test_plateau_lowers_projection_then_old_steps_expire(self):
        series = [(0, 0.67), (0.5, 0.68), (1, 0.69)]
        for point in [(2, 0.69), (4, 0.69), (6, 0.69)]:
            series.append(point)
        until, rate, _ = self.announcement(series)
        self.assertGreater(rate, 0)
        self.assertLess(until, self.reset)
        series.append((7, 0.69))
        until, rate, held = self.announcement(series)
        self.assertEqual(rate, 0)
        self.assertLess(until, self.reset)
        self.assertTrue(held)

    def test_return_under_pace_releases_without_hysteresis(self):
        # The elapsed share catches a plateau; confirmation never holds a turn.
        series = [(0, 0.61), (0.5, 0.62), (1, 0.63), (6, 0.63)]
        _, _, held = self.announcement(series)
        self.assertFalse(held)
