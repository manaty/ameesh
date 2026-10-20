# SPDX-License-Identifier: AGPL-3.0-only
"""#2816 : un débit plafonné par la pause des non-critiques ne fait pas une
prévision — l'alerte sous-utilisation ne part pas quand les agents sont tenus."""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import cost, sous_utilisation
from ameesh.config import Config

NOW = 1_800_000_000.0
HOUR = 3600.0
WEEK = 7 * 24 * HOUR


class HeldRateTest(unittest.TestCase):
    """26 % de la fenêtre écoulée : le plafond de pause d'un non-critique vaut
    26 % (rythme 36 %, réserve de 10 points). À 25,9 % utilisés, le compte
    vient de sortir de la bande, mais les six dernières heures ont été tenues.
    """

    def reading(self, gauge, used, hours=0):
        return {"harness": gauge.harness, "key": gauge.key, "account": "primaire",
                "used": used, "resets_at_ts": gauge.resets_at,
                "window_s": gauge.window_s, "observed_ts": NOW - hours * HOUR,
                "produced_ts": NOW - hours * HOUR}

    def alerts(self, gauge, history=()):
        cfg = Config(accounts={"codex": [{"name": "primaire"}]})
        ops = mock.Mock()
        ops.gauge_history.return_value = list(history)
        with mock.patch.object(sous_utilisation, "latest_account_gauges",
                               return_value={("codex", "primaire"): [(gauge, NOW)]}), \
             mock.patch.object(sous_utilisation.storage, "of") as storage_of:
            storage_of.return_value.operations = ops
            return sous_utilisation.plan_underused(None, [], NOW, cfg=cfg)

    def test_a_rate_capped_by_the_pause_does_not_alert(self):
        gauge = cost.Gauge("codex", "codex-10080min", 0.259,
                           NOW + 0.74 * WEEK, WEEK)
        history = [self.reading(gauge, 0.259, 6), self.reading(gauge, 0.259)]
        # Témoin : la moyenne de fenêtre projette 99,6 % utilisés, pas d'alerte.
        self.assertEqual(self.alerts(gauge), [])
        # Avant #2816, le débit nul des six heures tenues annonçait 74 % de
        # perte et l'alerte partait alors que les agents étaient tenus.
        self.assertEqual(self.alerts(gauge, history), [])

    def test_the_fallback_is_the_window_average(self):
        gauge = cost.Gauge("codex", "codex-10080min", 0.259,
                           NOW + 0.74 * WEEK, WEEK)
        lost, source, span = sous_utilisation._projected_plan_loss(
            gauge, "primaire", NOW,
            [self.reading(gauge, 0.259, 6), self.reading(gauge, 0.259)],
            multiplier=1.0, ceiling=cost.PACE_CEILING, reserve=0.10)
        self.assertEqual(source, "window_average")
        self.assertAlmostEqual(lost, 1 - 0.259 / 0.26, places=4)

    def test_an_unheld_recent_rate_still_alerts(self):
        gauge = cost.Gauge("codex", "codex-10080min", 0.05,
                           NOW + 0.40 * WEEK, WEEK)
        history = [self.reading(gauge, 0.02, 6), self.reading(gauge, 0.05)]
        [alert] = self.alerts(gauge, history)
        self.assertEqual(alert["rate_source"], "recent")
        self.assertGreater(alert["lost_at_pace"], 0.25)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
