# SPDX-License-Identifier: AGPL-3.0-only
"""#7333 : une horloge de widget en avance ne masque pas la lecture directe."""
import unittest
from unittest.mock import patch

from ameesh import cost


class GaugeClockTest(unittest.TestCase):
    def merge(self, local_source, history_source, delta, *, reset_delta=0,
              local_produced=1000):
        book = object.__new__(cost.CostBook)
        book.db = object()
        local = cost.Gauges([cost.Gauge(
            'claude', 'seven_day', 0.30, 9000, 604800,
            produced_at=local_produced, source=local_source)])
        row = dict(key='seven_day', used=0.36, resets_at_ts=9000 + reset_delta,
                   window_s=604800, produced_ts=1000 + delta,
                   source=history_source)
        with patch.object(book, '_gauge_latest', return_value=[row]):
            return book._gauges_with_history('secondaire', local)[0]

    def test_direct_read_beats_widget_clock_ahead_in_either_location(self):
        for delta in (0, 5, cost.GAUGE_CLOCK_JITTER_S):
            with self.subTest(delta=delta):
                direct = self.merge('ag1', 'widget', delta)
                self.assertEqual(direct.source, 'ag1')
                self.assertFalse(direct.from_history)
                direct = self.merge('widget', 'ag1', -delta)
                self.assertEqual(direct.source, 'ag1')
                self.assertTrue(direct.from_history)

    def test_genuinely_newer_widget_wins(self):
        widget = self.merge('ag1', 'widget', cost.GAUGE_CLOCK_JITTER_S + 1)
        self.assertEqual(widget.source, 'widget')
        self.assertTrue(widget.from_history)
        widget = self.merge('widget', 'ag1', -cost.GAUGE_CLOCK_JITTER_S - 1)
        self.assertEqual(widget.source, 'widget')
        self.assertFalse(widget.from_history)

    def test_other_sources_and_other_windows_keep_timestamp_order(self):
        for local, history, reset_delta in (
                ('ag1', 'ag2', 0), ('widget', 'widget', 0), ('', 'widget', 0),
                ('ag1', 'widget', cost.GAUGE_CLOCK_JITTER_S + 1)):
            with self.subTest(local=local, history=history, reset_delta=reset_delta):
                self.assertTrue(self.merge(local, history, 5,
                                           reset_delta=reset_delta).from_history)

    def test_undated_direct_read_does_not_override_dated_widget(self):
        self.assertEqual(self.merge('ag1', 'widget', 5,
                                    local_produced=None).source, 'widget')
