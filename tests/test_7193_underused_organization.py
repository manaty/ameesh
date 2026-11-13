# SPDX-License-Identifier: AGPL-3.0-only
"""#7193 : `plan_underused` lit `scope`, pas la réserve personnelle.

Constat de relecture de #7023 : `accounts.limits(reserve=True)` ne déduit
plus la réserve de 10 points pour un forfait `scope: organization` (borne
dure 95 %), mais `plan_underused` comparait encore chaque fenêtre à
`held_cap(RESERVE)` et filtrait les relevés de débit au même plafond. Une
fenêtre à 85 % avec un plafond de rythme à 90 % était donc annoncée comme une
pause alors que la garde la laissait passer : son alerte était perdue et les
relevés de la bande 80–95 % ne nourrissaient pas la projection de débit.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import cost, sous_utilisation
from ameesh.config import Config

NOW = 1_800_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR
WEEK = 7 * DAY


def _gauge(used, resets_in, key="codex-10080min", window=WEEK):
    return cost.Gauge("codex", key, used, NOW + resets_in, window)


class _Base(unittest.TestCase):
    scope = "organization"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-7193-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "partage")
        os.makedirs(self.home, mode=0o700)
        self.settings = {"name": "partage", "path": self.home,
                         "check_credentials": False}
        if self.scope:
            self.settings["scope"] = self.scope
        self.cfg = Config(state_dir=self.tmp, accounts={"codex": [self.settings]})

    def reading(self, gauge, used, seconds_ago):
        return {"harness": gauge.harness, "key": gauge.key, "account": "partage",
                "used": used, "resets_at_ts": gauge.resets_at,
                "window_s": gauge.window_s,
                "observed_ts": NOW - seconds_ago, "produced_ts": NOW - seconds_ago}

    def alerts(self, gauge, history=(), **thresholds):
        ops = mock.Mock()
        ops.gauge_history.return_value = list(history)
        with mock.patch.object(sous_utilisation, "latest_account_gauges",
                               return_value={("codex", "partage"): [(gauge, NOW)]}), \
                mock.patch.object(sous_utilisation.storage, "of") as storage_of:
            storage_of.return_value.operations = ops
            return sous_utilisation.plan_underused(None, [], NOW, cfg=self.cfg,
                                                   **thresholds)


class OrganizationUnderusedTest(_Base):
    def test_a_shared_window_at_85_percent_alerts(self):
        # 85 % utilisés à 95 % écoulés : 8 h avant la remise, 15 points
        # seront perdus. Le plafond de rythme est 90 %, la borne 95 % ; la
        # réserve personnelle de 10 points ne doit pas fermer l'alerte.
        gauge = _gauge(0.85, 8 * HOUR)
        [alerte] = self.alerts(gauge, used_pct=90, pace_gap_pct=10)
        self.assertEqual((alerte["gauge"], alerte["reason"]),
                         ("codex-10080min", "fin_de_fenetre"))
        self.assertAlmostEqual(alerte["pace_cap"], 0.90, places=4)
        self.assertAlmostEqual(alerte["lost"], 0.15, places=4)

    def test_readings_in_the_shared_band_feed_the_recent_rate(self):
        # Les trois relevés à 85 % sont dans la bande 80–95 % : au plafond de
        # pause personnel (80 %) ils seraient tous jetés et la projection
        # retomberait sur la moyenne de fenêtre (10,75 % de perte) ; sans
        # réserve ils donnent le débit récent (15 % de perte).
        gauge = _gauge(0.85, 8 * HOUR)
        history = [self.reading(gauge, 0.85, 6 * HOUR),
                   self.reading(gauge, 0.85, 3 * HOUR),
                   self.reading(gauge, 0.85, 0.0)]
        [alerte] = self.alerts(gauge, history, used_pct=90, pace_gap_pct=10)
        self.assertEqual(alerte["rate_source"], "recent")
        self.assertAlmostEqual(alerte["lost_at_pace"], 0.15, places=4)
        self.assertAlmostEqual(alerte["rate_span_s"], 6 * HOUR, places=0)

    def test_a_shared_plan_does_not_read_the_overage_book(self):
        # `overage_cap` (80 %) ne s'applique pas à un forfait d'organisation :
        # la borne dure de 95 % le remplace. Lire le journal d'overage pour
        # rien pouvait perdre l'alerte d'un compte à 85 %.
        gauge = _gauge(0.85, 8 * HOUR)
        with mock.patch.object(sous_utilisation.cost_mod, "CostBook",
                               side_effect=RuntimeError("journal illisible")):
            [alerte] = self.alerts(gauge, used_pct=90, pace_gap_pct=10)
        self.assertEqual((alerte["gauge"], alerte["reason"]),
                         ("codex-10080min", "fin_de_fenetre"))

    def test_the_shared_plan_cap_still_closes_capacity(self):
        # Contre-épreuve : au plafond de rythme (90 %), la capacité n'est pas
        # libre — l'alerte ne doit pas promettre la bande 90–95 %.
        self.assertEqual(self.alerts(_gauge(0.90, 8 * HOUR), used_pct=90, pace_gap_pct=10), [])


class PersonalUnderusedTest(_Base):
    scope = "personal"

    def test_a_personal_window_at_85_percent_keeps_its_reserve(self):
        # #7023 ne change que l'organisation : pour un compte personnel, la
        # même fenêtre à 85 % reste tenue par la pause à 80 %.
        self.assertEqual(self.alerts(_gauge(0.85, 8 * HOUR), used_pct=90, pace_gap_pct=10), [])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
