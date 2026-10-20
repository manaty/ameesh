# SPDX-License-Identifier: AGPL-3.0-only
"""#2757 : l'alerte dépend de la perte au débit réel, pas du plafond permis."""
from __future__ import annotations

import dataclasses
import unittest
from unittest import mock

from . import support
from ameesh import cost, sous_utilisation
from ameesh.config import Config

NOW = 1_800_000_000.0
HOUR = 3600.0
WEEK = 7 * 24 * HOUR


class UnderusedProjectionTest(unittest.TestCase):
    def gauge(self, used=0.41, elapsed=0.26):
        return cost.Gauge("codex", "codex-10080min", used, NOW + (1 - elapsed) * WEEK,
                          WEEK)

    def reading(self, gauge, used, hours=0, **extra):
        return {"harness": gauge.harness, "key": gauge.key, "account": "primaire",
                "used": used, "resets_at_ts": gauge.resets_at, "window_s": gauge.window_s,
                "observed_ts": NOW - hours * HOUR, "produced_ts": NOW - hours * HOUR,
                **extra}

    def alerts(self, gauge, history=(), **kwargs):
        cfg = Config(accounts={"codex": [{"name": "primaire", "pace_multiplier": 3.0}]})
        ops = mock.Mock()
        ops.gauge_history.return_value = list(history)
        with mock.patch.object(sous_utilisation, "latest_account_gauges", return_value={
                ("codex", "primaire"): [(gauge, NOW)]}), \
             mock.patch.object(sous_utilisation.storage, "of") as storage_of:
            storage_of.return_value.operations = ops
            return sous_utilisation.plan_underused(None, [], NOW, cfg=cfg, **kwargs)

    def test_41_percent_at_26_percent_elapsed_does_not_alert(self):
        gauge = self.gauge()
        self.assertEqual(self.alerts(gauge), [])
        self.assertEqual(self.alerts(gauge, [self.reading(gauge, 0.35, 6),
                                             self.reading(gauge, 0.41)]), [])

    def test_10_percent_at_60_percent_elapsed_alerts(self):
        gauge = self.gauge(0.10, 0.60)
        [alert] = self.alerts(gauge)
        self.assertAlmostEqual(alert["lost_at_pace"], 1 - 0.10 / 0.60, places=4)
        self.assertEqual(alert["rate_source"], "window_average")
        self.assertIn("projected loss", alert["detail"])

    def test_recent_acceleration_overrides_a_slow_window_average(self):
        gauge = self.gauge(0.10, 0.60)
        self.assertEqual(self.alerts(gauge, [self.reading(gauge, 0.01, 6),
                                           self.reading(gauge, 0.10)]), [])

    def test_recent_stall_overrides_a_fast_window_average(self):
        gauge = self.gauge()
        [alert] = self.alerts(gauge, [self.reading(gauge, 0.41, 6),
                                     self.reading(gauge, 0.41)])
        self.assertEqual(alert["lost_at_pace"], 0.59)
        self.assertEqual(alert["rate_source"], "recent")
        self.assertEqual(alert["rate_span_s"], 6 * HOUR)
        self.assertEqual(self.alerts(gauge, [self.reading(gauge, 0.41, 6),
                                            self.reading(gauge, 0.41)], pace_gap_pct=60), [])

    def test_history_is_scoped_to_the_same_account_bucket_and_window(self):
        gauge = self.gauge()
        for extra in ({"account": "secondaire"}, {"harness": "claude"},
                      {"key": "five_hour"}, {"resets_at_ts": gauge.resets_at - WEEK}):
            with self.subTest(extra=extra):
                self.assertEqual(self.alerts(gauge, [self.reading(gauge, 0.41, 6, **extra),
                                                     self.reading(gauge, 0.41)]), [])

    def test_echoes_and_short_history_do_not_create_a_stall(self):
        gauge = self.gauge()
        for baseline in (self.reading(gauge, 0.41, 6, produced_ts=NOW),
                         self.reading(gauge, 0.41, 0.1),
                         self.reading(gauge, 0.41, 7),
                         self.reading(gauge, 0.41, -1)):
            with self.subTest(baseline=baseline):
                self.assertEqual(self.alerts(gauge, [baseline, self.reading(gauge, 0.41)]), [])

    def test_recalibration_and_mismatching_latest_use_the_window_average(self):
        gauge = self.gauge()
        for history in ([self.reading(gauge, 0.5, 6), self.reading(gauge, 0.41)],
                        [self.reading(gauge, 0.1, 6), self.reading(gauge, 0.11)]):
            self.assertEqual(self.alerts(gauge, history), [])

    def test_projected_loss_threshold_is_strict_and_zero_disables_the_pace_reason(self):
        gauge = self.gauge(0.375, 0.50)  # exactement 25 % perdus
        self.assertEqual(self.alerts(gauge), [])
        self.assertEqual(len(self.alerts(gauge, pace_gap_pct=24)), 1)
        self.assertEqual(self.alerts(gauge, pace_gap_pct=0), [])

    def test_undated_production_uses_observation_time(self):
        gauge = self.gauge()
        [alert] = self.alerts(gauge, [self.reading(gauge, 0.41, 6, produced_ts=None),
                                     self.reading(gauge, 0.41, produced_ts=None)])
        self.assertEqual(alert["rate_source"], "recent")

    def test_tail_also_suppresses_an_account_projected_to_be_consumed(self):
        gauge = dataclasses.replace(self.gauge(0.10, 0.90), key="five_hour",
                                    window_s=5 * HOUR, resets_at=NOW + HOUR / 2)
        [alert] = self.alerts(gauge, [self.reading(gauge, 0.0, 0.5),
                                      self.reading(gauge, 0.10)])
        self.assertEqual(alert["reason"], "fin_de_fenetre")
        # Avec un débit suffisant pour consommer avant remise, aucune alerte.
        gauge = dataclasses.replace(gauge, used=0.49)
        self.assertEqual(self.alerts(gauge, [self.reading(gauge, 0.0, 0.5),
                                           self.reading(gauge, 0.49)]), [])
