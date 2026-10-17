# SPDX-License-Identifier: AGPL-3.0-only
"""#2233 : une fenêtre peu utilisée ne promet pas un compte au plafond."""
from __future__ import annotations

import dataclasses
import unittest
from unittest import mock

from . import support
from ameesh import cost, sous_utilisation
from ameesh.config import Config


NOW = 1_800_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR


class UnderusedLimitsTest(unittest.TestCase):
    def setUp(self):
        self.profile = {"name": "secondaire", "path": "/unused/secondaire",
                        "pace_multiplier": 3.0}
        self.cfg = Config(state_dir="/unused/state", accounts={"codex": [self.profile]})
        self.short = cost.Gauge("codex", "codex-300min", 0.04, NOW + HOUR / 2, 5 * HOUR)
        self.long = cost.Gauge("codex", "codex-10080min", 0.83, NOW + 2 * DAY, 7 * DAY)

    def alerts(self, *, profile=None, long=None, overage=cost.Overage(capable=True),
               extra=None, profiles=None, unreadable=False):
        cfg = dataclasses.replace(self.cfg, accounts={
            "codex": profiles or [profile or self.profile]})
        gauges = [self.short, self.long if long is None else long]
        readings = {("codex", "secondaire"): [(g, NOW) for g in gauges]}
        if extra:
            readings.update(extra)
        book = mock.Mock()
        book.codex_gauges.return_value = cost.Gauges(gauges, overage)
        if unreadable:
            book.codex_gauges.side_effect = OSError("journal unreadable")
        with mock.patch.object(sous_utilisation, "latest_account_gauges",
                               return_value=readings), \
             mock.patch.object(cost, "CostBook", return_value=book):
            out = sous_utilisation.plan_underused(None, [], NOW, cfg=cfg)
        book.record_gauges.assert_not_called()
        return out

    def test_long_window_overage_cap_mutes_unused_short_window(self):
        # Incident #97605 : 4 % sur 5 h, mais 83 % sur 7 j et plafond 80 %.
        self.assertEqual(self.alerts(), [])

    def test_overage_threshold_reset_and_capability(self):
        for used, reset, capable, expected in (
                (0.80, NOW + 2 * DAY, True, []),
                (0.79, NOW + 2 * DAY, True, ["codex-300min"]),
                (0.95, NOW - 60, True, ["codex-300min"]),
                (0.83, NOW + 2 * DAY, False, ["codex-300min"]),
                (0.83, NOW + 2 * DAY, None, ["codex-300min"])):
            with self.subTest(used=used, reset=reset, capable=capable):
                long = dataclasses.replace(self.long, used=used, resets_at=reset)
                out = self.alerts(long=long, overage=cost.Overage(capable=capable))
                self.assertEqual([a["gauge"] for a in out], expected)

    def test_declared_fixed_caps_also_mute_the_alert(self):
        for cap, overage in ((0.50, "none"), (0.10, "none"), (None, "allowed")):
            with self.subTest(cap=cap, overage=overage):
                profile = {**self.profile, "cap": cap, "overage": overage}
                self.assertEqual(self.alerts(profile=profile), [])

    def test_a_blocked_account_does_not_mute_another_account(self):
        out = self.alerts(extra={
            ("codex", "other"): [(self.short, NOW)]}, profiles=[
                self.profile, {**self.profile, "name": "other", "path": "/unused/other"}])
        self.assertEqual([(a["account"], a["gauge"]) for a in out],
                         [("other", "codex-300min")])

    def test_a_model_bucket_does_not_block_general_capacity(self):
        model = dataclasses.replace(self.long, key="seven_day@fable", scope="fable",
                                    used=0.95)
        out = self.alerts(long=model)
        self.assertEqual([a["gauge"] for a in out], ["codex-300min"])

    def test_short_window_cap_mutes_the_underused_week(self):
        self.short = dataclasses.replace(self.short, used=0.76)
        long = dataclasses.replace(self.long, used=0.20)
        profile = {**self.profile, "short_window_cap": 0.75, "overage": "none"}
        self.assertEqual(self.alerts(profile=profile, long=long), [])

    def test_undated_blocking_window_is_not_usable_capacity(self):
        self.assertEqual(self.alerts(long=dataclasses.replace(self.long, resets_at=None)), [])

    def test_unreadable_overage_evidence_does_not_promise_capacity(self):
        self.assertEqual(self.alerts(unreadable=True), [])


if __name__ == "__main__":
    unittest.main()
