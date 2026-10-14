# SPDX-License-Identifier: AGPL-3.0-only
"""#1227 — suites de #1176 (relecture Claude R1).

N1 : `_release` annonçait `from_ts` (le début du relèvement de fin de fenêtre)
sans vérifier que le rythme y avait rejoint l'usage. Une fenêtre de 7 jours
relevée 48 h avant sa remise à zéro voit un rythme de ~81 % à `from_ts` : un
usage à 85 % reste tenu après le relèvement, et l'heure annoncée doit être le
croisement sous le plafond relevé, pas `from_ts`.

N3 : les lecteurs d'affichage (`sous_utilisation.plan_underused`,
`progress.build_budget`) gardaient le plafond par défaut (90 %, ×1) au lieu du
réglage du compte (`pace_ceiling`, `pace_multiplier`), et contredisaient
`limits`.

Aucune base pour N1 et pour `progress` : les jauges sont des objets
`cost.Gauge`, les profils des configurations en mémoire.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import time
import unittest

from ameesh import accounts, cost, progress, sous_utilisation, storage

from .support import PgTestCase

HOUR = 3600.0
WEEK = 7 * 24 * HOUR
NOW = 1_800_000_000.0


def _gauge(used: float, *, before_reset: float, window: float = WEEK) -> cost.Gauge:
    return cost.Gauge("codex", "codex-10080min", used, NOW + before_reset, window)


class _Book:
    """Des jauges données, sans base ni écriture."""

    db = None

    def __init__(self, gauges):
        self._gauges = cost.Gauges(list(gauges), cost.Overage(False))

    def codex_gauges(self, sessions=None):
        return self._gauges

    def record_gauges(self, gauges, account=None):
        return 0


class UntilUnderRaiseTest(unittest.TestCase):
    """N1 : l'heure `until` ne précède jamais le croisement du rythme."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-1227-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        directory = os.path.join(self.tmp, "compte")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        raw = {"name": "secondaire", "path": directory, "check_credentials": False,
               "pace_ceiling": 0.90, "pace_ceiling_end": 0.98,
               "pace_ceiling_end_hours": 48}
        self.profile = accounts.parse({"codex": [raw]})["codex"][0]
        self.items = [self.profile]

    def _evaluate(self, gauge, **kw):
        ev = accounts.evaluate(_Book([gauge]), self.profile, self.items, NOW, **kw)
        self.assertFalse(ev.ok, ev.reason)
        return ev

    def test_the_raise_waits_for_the_pace_to_catch_up(self):
        """Usage 85 %, rythme 81 % à `from_ts` : l'heure est le croisement."""
        gauge = _gauge(0.85, before_reset=60 * HOUR)
        ev = self._evaluate(gauge, critical=True)
        from_ts = gauge.resets_at - 48 * HOUR
        crossing = gauge.resets_at - WEEK * (1 - (0.85 - cost.PACE_MARGIN))
        self.assertGreater(crossing, from_ts)
        self.assertAlmostEqual(ev.until, crossing)
        self.assertNotAlmostEqual(ev.until, from_ts, places=3)

    def test_above_the_base_ceiling_the_raised_pace_still_holds(self):
        """Usage 93 % (au-dessus des 90 % de base, sous les 98 % relevés)."""
        gauge = _gauge(0.93, before_reset=60 * HOUR)
        ev = self._evaluate(gauge, critical=True)
        from_ts = gauge.resets_at - 48 * HOUR
        crossing = gauge.resets_at - WEEK * (1 - (0.93 - cost.PACE_MARGIN))
        self.assertGreater(crossing, from_ts)
        self.assertAlmostEqual(ev.until, crossing)

    def test_the_reserve_keeps_its_own_crossing_after_the_raise(self):
        """Réserve : le croisement se calcule sous le plafond relevé, moins la réserve."""
        gauge = _gauge(0.85, before_reset=60 * HOUR)
        ev = self._evaluate(gauge, reserve=True)
        crossing = gauge.resets_at - WEEK * (1 - 0.85)
        self.assertAlmostEqual(ev.until, crossing)

    def test_a_crossing_before_the_raise_keeps_its_earlier_hour(self):
        """Rythme déjà rattrapé avant `from_ts` : l'heure reste celle du croisement."""
        gauge = _gauge(0.80, before_reset=60 * HOUR)
        ev = self._evaluate(gauge, critical=True)
        from_ts = gauge.resets_at - 48 * HOUR
        crossing = gauge.resets_at - WEEK * (1 - (0.80 - cost.PACE_MARGIN))
        self.assertLess(crossing, from_ts)
        self.assertAlmostEqual(ev.until, crossing)

    def test_above_the_raised_ceiling_the_reset_still_wins(self):
        """Usage 99 % : le plafond relevé (98 %) ne rattrape pas, remise à zéro."""
        gauge = _gauge(0.99, before_reset=60 * HOUR)
        ev = self._evaluate(gauge, critical=True)
        self.assertAlmostEqual(ev.until, gauge.resets_at)


class ProgressPaceSettingsTest(unittest.TestCase):
    """N3 : `progress` lit le plafond effectif du compte déclaré.

    #1227 × #1342 : le plan d'un harnais à comptes déclarés est celui de son
    compte ACTIF (#1028 P2) ; le plafond effectif (`pace_ceiling`, #1176) se
    lit sur la même ligne de compte.
    """

    def _account(self, gauge, **over):
        """Une ligne d'`accounts.report` : le compte actif porte ses jauges."""
        row = {"harness": "codex", "account": "secondaire", "active": True,
               "pace_multiplier": 2.0,
               "gauges": [{"key": gauge.key, "pace_ceiling": 0.98,
                           "ceiling_reason": accounts.CEILING_REASON_END,
                           "used": gauge.used, "last_used": gauge.used, "cap": 0.98,
                           "elapsed": gauge.elapsed(NOW), "resets_at": gauge.resets_at,
                           "reset_passed": False}],
               "_gauges": [gauge]}
        row.update(over)
        return row

    def test_the_effective_ceiling_is_used(self):
        gauge = _gauge(0.95, before_reset=3 * HOUR)
        b = progress.build_budget([], [gauge], NOW, accounts=[self._account(gauge)])
        plan = b["plans"][0]
        self.assertAlmostEqual(plan["pace_cap"], 0.98)
        self.assertFalse(plan["exceeded"])          # 95 % sous le plafond relevé

    def test_without_a_declared_account_the_default_holds(self):
        gauge = _gauge(0.95, before_reset=3 * HOUR)
        b = progress.build_budget([], [gauge], NOW)
        plan = b["plans"][0]
        self.assertAlmostEqual(plan["pace_cap"], cost.PACE_CEILING)
        self.assertTrue(plan["exceeded"])           # 95 % au-dessus des 90 % par défaut

    def test_declared_accounts_without_an_active_one_keep_the_default(self):
        # #1028 P2 : deux comptes déclarés qui divergent, aucun actif -> la
        # jauge et le plafond restent nominaux, jamais ceux d'un compte tiré au
        # hasard (l'ancien mécanisme #1227 `_effective_pace`).
        gauge = _gauge(0.95, before_reset=3 * HOUR)
        secondaire = self._account(gauge, active=False, account="secondaire")
        tertiaire = self._account(gauge, active=False, account="tertiaire")
        tertiaire["gauges"] = [dict(tertiaire["gauges"][0], pace_ceiling=0.92, cap=0.90)]
        b = progress.build_budget([], [gauge], NOW, accounts=[secondaire, tertiaire])
        plan = b["plans"][0]
        self.assertAlmostEqual(plan["pace_cap"], cost.PACE_CEILING)
        self.assertTrue(plan["exceeded"])


class UnderusedPaceSettingsTest(PgTestCase):
    """N3 : `plan_underused` lit le plafond du compte déclaré, pas les 90 % en dur."""

    def setUp(self):
        super().setUp()
        self.now = time.time()

    def _record(self, used, resets_in, *, account=None, window=WEEK):
        storage.of(self.db).operations.record_gauges([{
            "harness": "codex", "key": "codex-10080min", "used": used,
            "resets_at": self.now + resets_in, "window_s": window,
            "account": account}])

    def _cfg(self, comptes):
        return dataclasses.replace(self.cfg, accounts={"codex": comptes})

    def _plan(self, cfg, gap=8.0):
        return sous_utilisation.plan_underused(
            self.db, [], self.now, tail_s=0, used_pct=0, pace_gap_pct=gap, cfg=cfg)

    def test_the_declared_ceiling_raises_the_allowed_pace(self):
        # 85 % écoulés, 85 % utilisés : défaut 90 % (écart 5), compte 98 % (écart 10)
        self._record(0.85, 0.15 * WEEK, account="secondaire")
        profile = {"name": "secondaire", "path": "/x/compte", "check_credentials": False,
                   "pace_ceiling": 0.98}
        self.assertEqual(self._plan(self._cfg([]), gap=8.0), [])
        [alerte] = self._plan(self._cfg([profile]), gap=8.0)
        self.assertEqual((alerte["account"], alerte["reason"]), ("secondaire", "sous_rythme"))
        self.assertAlmostEqual(alerte["pace_cap"], 0.95, places=4)
        self.assertIn("allowed pace of 95 %", alerte["detail"])

    def test_the_declared_multiplier_raises_the_allowed_pace(self):
        # 40 % écoulés : défaut 50 %, compte ×2 -> 90 %
        self._record(0.45, 0.6 * WEEK, account="secondaire")
        profile = {"name": "secondaire", "path": "/x/compte", "check_credentials": False,
                   "pace_multiplier": 2.0}
        self.assertEqual(self._plan(self._cfg([]), gap=8.0), [])
        [alerte] = self._plan(self._cfg([profile]), gap=8.0)
        self.assertAlmostEqual(alerte["pace_cap"], 0.90, places=4)

    def test_a_reading_without_an_account_and_several_accounts_keeps_the_default(self):
        self._record(0.85, 0.15 * WEEK)
        profils = [{"name": name, "path": "/x/%s" % name, "check_credentials": False,
                    "pace_ceiling": 0.98} for name in ("primaire", "secondaire")]
        self.assertEqual(self._plan(self._cfg(profils), gap=8.0), [])

    def test_an_unknown_account_keeps_the_default(self):
        self._record(0.85, 0.15 * WEEK, account="inconnu")
        profile = {"name": "secondaire", "path": "/x/compte", "check_credentials": False,
                   "pace_ceiling": 0.98}
        self.assertEqual(self._plan(self._cfg([profile]), gap=8.0), [])


if __name__ == "__main__":
    unittest.main()
