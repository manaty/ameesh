# SPDX-License-Identifier: AGPL-3.0-only
"""#2648 — l'alerte de forfait sous-utilisé et le « next choice » d'`accounts
list` appliquent l'éligible de la pause d'un agent non critique.

Constat du 19/10 : `plan_underused` annonçait 76 % de la fenêtre 5 h de
claude/tertiaire perdus au reset alors que la même lecture, côté pause, jugeait
le compte au seuil sur `seven_day` (75 % à 71 % d'écoulé, plafond de rythme
80 % − 10 points de réserve) ; `accounts list` le donnait aussi « next choice ».
Les deux lectures ne doivent proposer que ce qu'un agent non critique peut
réellement prendre : toutes les fenêtres sous leur plafond effectif, réserve
comprise (L242b). Un compte sous la réserve reste, lui, proposé.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import accounts, cost, sous_utilisation
from ameesh.config import Config

NOW = 1_800_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR
WEEK = 7 * DAY


def _gauge(used, resets_in, key="codex-10080min", window=WEEK):
    return cost.Gauge("codex", key, used, NOW + resets_in, window)


class _Book:
    """Des jauges données, sans base ni écriture."""

    db = None

    def __init__(self, gauges):
        self._gauges = list(gauges)

    def codex_gauges(self, sessions=None, *, plan=None):
        return cost.Gauges(self._gauges)

    def record_gauges(self, gauges, account=None):
        return 0


class NextChoiceReserveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2648-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "secondaire")
        os.makedirs(self.home, mode=0o700)
        os.chmod(self.home, 0o700)
        self.cfg = Config(state_dir=self.tmp, accounts={"codex": [
            {"name": "secondaire", "path": self.home, "check_credentials": False}]})
        self.items = accounts.parse(self.cfg.accounts)["codex"]
        self.profile = self.items[0]
        # 5 h presque finie, peu utilisée : la fenêtre qui « perd » son forfait.
        self.short = _gauge(0.04, 0.5 * HOUR, "codex-300min", 5 * HOUR)
        # 7 j à 71 % d'écoulé : rythme ordinaire 81 %, réserve 71 %.
        self.long_blocked = _gauge(0.75, 2 * DAY)
        self.long_free = _gauge(0.60, 2 * DAY)

    def _alerts(self, gauges):
        readings = {("codex", "secondaire"): [(g, NOW) for g in gauges]}
        book = mock.Mock()
        book.codex_gauges.return_value = cost.Gauges(gauges)
        with mock.patch.object(sous_utilisation, "latest_account_gauges",
                               return_value=readings), \
                mock.patch.object(cost, "CostBook", return_value=book):
            return sous_utilisation.plan_underused(None, [], NOW, cfg=self.cfg)

    def _rows(self, gauges):
        return accounts.report(self.cfg, None, _Book(gauges), now=NOW, record=False)

    def test_reserve_band_blocks_the_alert_and_the_next_choice(self):
        # Le compte est dans la bande de réserve : au seuil pour un
        # non-critique, encore sous le plafond ordinaire.
        ev = accounts.evaluate(_Book([self.short, self.long_blocked]),
                               self.profile, self.items, NOW, reserve=True)
        self.assertFalse(ev.ok)
        self.assertIn("10 points kept for critical agents", ev.reason)
        ev_critical = accounts.evaluate(_Book([self.short, self.long_blocked]),
                                        self.profile, self.items, NOW)
        self.assertTrue(ev_critical.ok)      # bande : pas un compte saturé

        self.assertEqual(self._alerts([self.short, self.long_blocked]), [])
        [row] = self._rows([self.short, self.long_blocked])
        self.assertFalse(row["ok"], row["reason"])
        self.assertFalse(row["next"], row.get("why"))
        self.assertIn("codex-10080min", row["reason"])

    def test_below_the_reserve_the_capacity_is_still_offered(self):
        self.assertTrue(accounts.evaluate(
            _Book([self.short, self.long_free]), self.profile, self.items,
            NOW, reserve=True).ok)
        [alerte] = self._alerts([self.short, self.long_free])
        self.assertEqual(alerte["gauge"], "codex-300min")
        [row] = self._rows([self.short, self.long_free])
        self.assertTrue(row["ok"], row["reason"])
        self.assertTrue(row["next"], row.get("why"))
        self.assertIn("pace 71%", row["why"])

    def test_the_alert_quotes_the_non_critical_pace(self):
        """Le rythme annoncé est celui de la pause (réserve déduite), pas le
        plafond ordinaire : un écart calculé sur 81 % promettrait 11 points
        de plus que la pause."""
        # fenêtre longue à 71 % d'écoulé, 30 % utilisés : sous-rythme.
        [alerte] = self._alerts([_gauge(0.30, 2 * DAY)])
        self.assertAlmostEqual(alerte["pace_cap"], 0.7142857, places=4)
        self.assertIn("projected loss 58 %", alerte["detail"])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
