# SPDX-License-Identifier: AGPL-3.0-only
"""#5408 : pas d'alerte de forfait sous-utilisé quand la jauge est au plafond
de cadence des non-critiques.

Le 2026-11-01 à 22:25:45Z, `plan_underused` a annoncé « account claude/primaire,
window seven_day: 63 % used, projected loss 37 % … idle claude agents: … ».
Le même relevé, côté pause, vaut 63 % utilisés pour un plafond de 63,35 % :
0,35 point d'avance, moins que le pas d'un relevé (l'utilisation est publiée
en points entiers), donc aucune capacité prouvée libre ; et les six dernières
heures de relevés, tous pris à 63 % pendant que la pause tenait les agents,
donnaient un débit nul qui projetait 37 % de perte « au rythme courant ».

Deux gardes :
* la comparaison au plafond de pause laisse un point de résolution
  (`GAUGE_STEP`), avec ou sans compte déclaré ;
* un relevé à moins d'un point du plafond est un relevé de pause : il ne
  nourrit pas le débit récent et la projection retombe sur la moyenne de
  fenêtre.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import cost, sous_utilisation
from ameesh.config import Config

#: 2026-11-01T22:25:45Z, l'instant de l'alerte #182676.
NOW = 1_793_571_945.0
#: 2026-11-04T12:00:00Z, la remise à zéro du seven_day de claude/primaire.
RESET = 1_793_793_600.0
WEEK = 7 * 86400.0
HOUR = 3600.0


class UnderusedAtPauseCapTest(unittest.TestCase):
    def gauge(self, used=0.63):
        return cost.Gauge("claude", "seven_day", used, RESET, WEEK)

    def reading(self, gauge, used, seconds_ago=0):
        return {"harness": gauge.harness, "key": gauge.key, "account": "primaire",
                "used": used, "resets_at_ts": gauge.resets_at,
                "window_s": gauge.window_s,
                "observed_ts": NOW - seconds_ago, "produced_ts": NOW - seconds_ago}

    def alerts(self, gauge, history=(), cfg=None):
        ops = mock.Mock()
        ops.gauge_history.return_value = list(history)
        with mock.patch.object(sous_utilisation, "latest_account_gauges", return_value={
                ("claude", "primaire"): [(gauge, NOW)]}), \
             mock.patch.object(sous_utilisation.storage, "of") as storage_of:
            storage_of.return_value.operations = ops
            return sous_utilisation.plan_underused(None, [], NOW, cfg=cfg)

    def cfg(self):
        return Config(accounts={"claude": [{"name": "primaire",
                                            "cap": 0.9, "path": "/unused/primaire"}]})

    def held_history(self, gauge):
        """Les relevés réels de 21:50:42 à 22:24:03, tous à 63 %."""
        return [self.reading(gauge, 0.63, seconds_ago) for seconds_ago in
                (2012, 1989, 1287, 688, 360, 186, 1)]

    def test_the_22h26_reading_does_not_alert(self):
        # Reproduit avant correctif : 1 alerte, `lost_at_pace` 0,37, débit
        # « recent » sur 33 min. La jauge est à 0,35 point du plafond de pause.
        gauge = self.gauge()
        self.assertAlmostEqual(gauge.held_cap(NOW, 0.10), 0.6335, places=4)
        self.assertEqual(self.alerts(gauge, self.held_history(gauge), cfg=self.cfg()), [])

    def test_a_real_underuse_below_the_cap_still_alerts(self):
        # Contre-épreuve : 50 % utilisés à 63 % d'écoulé (13 points sous le
        # plafond), débit nul sur six heures -> 50 % de perte, alerte.
        gauge = self.gauge(0.50)
        history = [self.reading(gauge, 0.50, seconds_ago) for seconds_ago in
                   (6 * HOUR, 3 * HOUR, 1)]
        [alert] = self.alerts(gauge, history, cfg=self.cfg())
        self.assertEqual(alert["rate_source"], "recent")
        self.assertAlmostEqual(alert["lost_at_pace"], 0.50, places=4)

    def test_a_reading_at_the_pause_cap_does_not_feed_the_recent_rate(self):
        gauge = self.gauge()
        lost, source, _span = sous_utilisation._projected_plan_loss(
            gauge, "primaire", NOW, self.held_history(gauge),
            multiplier=1.0, ceiling=cost.PACE_CEILING, reserve=0.10)
        self.assertEqual(source, "window_average")
        self.assertAlmostEqual(lost, 1 - 0.63 / gauge.elapsed(NOW), places=4)

    def test_without_a_declared_account_the_pause_cap_still_holds(self):
        # `caps` vide (compte inconnu) ne doit pas rouvrir la capacité : la
        # pause s'applique au harnais, pas à la déclaration.
        gauge = self.gauge()
        self.assertEqual(self.alerts(gauge, self.held_history(gauge), cfg=None), [])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
