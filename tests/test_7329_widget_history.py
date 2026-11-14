# SPDX-License-Identifier: AGPL-3.0-only
"""#7329 — la lecture du widget entre dans l'historique partagé.

Constat (AG1, 14/11 05:29Z) : le poste lit frais la jauge seven_day du compte
Claude secondaire (widget, 05:28Z, 36 %, plafond 40 %), AG1 garde un relevé
partiel de 01:22Z (34 %, plafond 23 %) et met le compte en pause. Le relevé du
widget était publié avec `produced_at` NULL : `_gauges_with_history` ne retient
que les lignes datées, donc aucun autre hôte ne pouvait le reprendre.

Témoins :
* la lecture du widget porte sa propre date comme instant de production ;
* le même relevé, publié par un hôte, complète la lecture périmée d'AG1 :
  `stale_reading` devient faux et le plafond passe de 23 % à 40 % ;
* une lecture directe plus récente que le widget le bat encore : le widget
  n'est pas une source de vérité plus récente qu'une lecture directe.
"""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone

from ameesh import accounts, cost, storage, widget_usage

from .support import PgTestCase
from .test_cost import Sandbox
from .test_1132_jauges_perimees import line, marked, windows

HOUR = 3600.0
WEEK = 7 * 24 * HOUR
#: la part écoulée du relevé d'AG1 (01:22Z) : 23,53 %
ELAPSED = 0.2353


class WidgetHistoriqueTest(Sandbox, PgTestCase):
    """Le relevé du widget publié sert les autres hôtes, sans les dominer."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE quota_gauge_readings RESTART IDENTITY")
        self.profile = accounts.Profile(
            "claude", "secondaire",
            path=os.path.join(self.tmp_cost, "compte"),
            underused_loss=0.5)

    def _lecture_ag1(self) -> cost.Gauges:
        """Le relevé partiel d'AG1 : la fenêtre 5 h s'est remise à zéro."""
        self.agent("ag1", "claude")
        self.events("ag1", marked("secondaire", self.now - 4 * HOUR),
                    windows(five_hour=(0.06, self.now - HOUR),
                            seven_day=(0.34, self.now + (1.0 - ELAPSED) * WEEK)))
        return self.book().claude_gauges(account="secondaire", primary="primaire")

    def _publie_le_widget(self, produced: float | None = None) -> None:
        stamp = self.now - 60 if produced is None else produced
        storage.of(self.db).operations.record_gauges([
            line("claude", "five_hour", 0.06, self.now + 2 * HOUR,
                 account="secondaire", produced_at=stamp, source="widget"),
            line("claude", "seven_day", 0.36, self.now + (1.0 - ELAPSED) * WEEK,
                 account="secondaire", produced_at=stamp, source="widget"),
        ])

    def _cap(self, gauges) -> float:
        return accounts.limits(self.profile, gauges, self.now,
                               reserve=True)["seven_day"][0]

    def test_la_lecture_du_widget_publie_sa_date_dans_l_historique(self):
        """La date du relevé voyage jusqu'à la ligne partagée, pas l'insertion."""
        stamp = self.now - 60
        dossier = os.path.join(self.tmp_cost, "widget")
        os.makedirs(dossier, exist_ok=True)
        with open(os.path.join(dossier, "claude-secondary.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({
                "schemaVersion": 1, "ready": True,
                "updatedAt": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                "limits": [
                    {"label": "Session (5-hour)", "percent": 0.06,
                     "resetsAt": datetime.fromtimestamp(
                         self.now + 2 * HOUR, timezone.utc).isoformat()},
                    {"label": "Weekly (7-day)", "percent": 0.36,
                     "resetsAt": datetime.fromtimestamp(
                         self.now + 5 * 86400, timezone.utc).isoformat()},
                ]}, fh)
        gauges = widget_usage.read(self.profile, now=self.now, directory=dossier,
                                   key="claude-secondary")
        self.assertIsNotNone(gauges)
        self.book().record_gauges(gauges, account="secondaire")
        rows = storage.of(self.db).operations.gauge_history(
            since_s=3600, harness="claude", account="secondaire")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertAlmostEqual(row["produced_ts"], stamp, places=0)
            self.assertEqual(row["source"], "widget")

    def test_le_releve_perime_seul_plafonne_a_23_pour_cent(self):
        """Avant le correctif : 0,235 et une pause, comme sur AG1."""
        gauges = self._lecture_ag1()
        self.assertTrue(cost.stale_reading(gauges, self.now))
        jauge = {g.key: g for g in gauges}["seven_day"]
        self.assertIsNone(accounts.underused_floor(
            self.profile, gauges, jauge, self.now))
        self.assertAlmostEqual(self._cap(gauges), ELAPSED, places=3)

    def test_le_widget_publie_complete_ag1_et_donne_40_pour_cent(self):
        """Le même relevé, vu par AG1 : plafond 40 % au lieu de 23 %."""
        self._lecture_ag1()
        self._publie_le_widget()
        gauges = self.book().claude_gauges(account="secondaire",
                                           primary="primaire")
        jauge = {g.key: g for g in gauges}["seven_day"]
        self.assertTrue(jauge.from_history)
        self.assertEqual(jauge.source, "widget")
        self.assertAlmostEqual(jauge.produced_at, self.now - 60, places=0)
        self.assertFalse(cost.stale_reading(gauges, self.now))
        self.assertAlmostEqual(accounts.underused_floor(
            self.profile, gauges, jauge, self.now), 0.5)
        self.assertAlmostEqual(self._cap(gauges), 0.40, places=3)

    def test_une_lecture_directe_plus_recente_bat_le_widget(self):
        """Le widget ne prime jamais sur une lecture directe plus récente."""
        self._publie_le_widget(produced=self.now - 3 * HOUR)
        self.agent("ag2", "claude")
        self.events("ag2", marked("secondaire", self.now - 60),
                    windows(five_hour=(0.06, self.now + 2 * HOUR),
                            seven_day=(0.30, self.now + (1.0 - ELAPSED) * WEEK)))
        gauges = self.book().claude_gauges(account="secondaire",
                                           primary="primaire")
        jauge = {g.key: g for g in gauges}["seven_day"]
        self.assertFalse(jauge.from_history)
        self.assertAlmostEqual(jauge.used, 0.30)
        self.assertAlmostEqual(jauge.produced_at, self.now - 60, places=0)


if __name__ == "__main__":
    unittest.main()
