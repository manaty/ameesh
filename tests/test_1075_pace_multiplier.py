# SPDX-License-Identifier: AGPL-3.0-only
"""#1075 — multiplicateur de rythme par profil compte (bureau, 13/10).

Le compte principal Codex porte `pace_multiplier: 2.0` dans la configuration de
l'hôte : son plafond de rythme vise la part écoulée deux fois plus vite, sans
toucher aux jauges, aux remises à zéro, à la réserve ni aux plafonds fixes.
Aucun défaut implicite : sans réglage, tous les comptes restent à 1.0.
"""
from __future__ import annotations

import math
import os
import shutil
import tempfile
import unittest

from ameesh import accounts, cost

HOUR = 3600.0
WEEK = 7 * 24 * HOUR


def _gauge(used: float, now: float, elapsed: float, key: str = "codex-300min",
           window: float = 5 * HOUR) -> cost.Gauge:
    return cost.Gauge("codex", key, used, now + (1.0 - elapsed) * window, window)


class _Book:
    """Un livre dont les jauges Codex sont données par dossier de sessions."""

    db = None

    def __init__(self, gauges: dict):
        self._gauges = gauges

    def codex_gauges(self, sessions=None):
        return self._gauges.get(sessions, [])

    def record_gauges(self, gauges, account=None):
        return 0


class PaceMultiplierSettingTest(unittest.TestCase):
    """Le réglage est propre à chaque compte, sans défaut implicite."""

    def _items(self, **settings):
        raw = [{"name": "primaire"}, {"name": "secondaire", "path": "/x/codex-2"}]
        for item in raw:
            item.update(settings.get(item["name"], {}))
        return accounts.parse({"codex": raw})["codex"]

    def test_absent_is_nominal_everywhere(self):
        primary, secondary = self._items()
        self.assertEqual((primary.pace_multiplier, secondary.pace_multiplier), (1.0, 1.0))
        # Codex 2 et le primaire Claude gardent le rythme nominal : le défaut
        # ne dépend ni du nom, ni du dossier par défaut.
        claude = accounts.parse({"claude": [{"name": "primaire"}]})["claude"][0]
        self.assertEqual(claude.pace_multiplier, 1.0)

    def test_setting_parsed_per_account(self):
        primary, secondary = self._items(primaire={"pace_multiplier": 2},
                                         secondaire={"pace_multiplier": 0.5})
        self.assertEqual(primary.pace_multiplier, 2.0)
        self.assertEqual(secondary.pace_multiplier, 0.5)

    def test_invalid_settings_refused(self):
        for bad in ({"pace_multiplier": 0}, {"pace_multiplier": -1},
                    {"pace_multiplier": "x"}, {"pace_multiplier": True},
                    {"pace_multiplier": math.inf}, {"pace_multiplier": math.nan}):
            with self.assertRaises(accounts.AccountError, msg=bad):
                self._items(primaire=bad)


class PaceCapTest(unittest.TestCase):
    """`Gauge.pace_cap` : seule la part écoulée est multipliée."""

    def test_multiplier_scales_the_elapsed_share(self):
        now = 1_800_000_000.0
        gauge = _gauge(0.20, now, elapsed=0.10)
        self.assertAlmostEqual(gauge.pace_cap(now), 0.20)
        self.assertAlmostEqual(gauge.pace_cap(now, 2.0), 0.30)   # 2 × 10 % + 10 pts
        self.assertAlmostEqual(gauge.elapsed(now), 0.10)          # jamais multipliée
        self.assertEqual(gauge.used, 0.20)                        # jamais touchée

    def test_ceiling_and_margin_unchanged(self):
        now = 1_800_000_000.0
        start = _gauge(0.0, now, elapsed=0.0)
        self.assertAlmostEqual(start.pace_cap(now, 2.0), cost.PACE_MARGIN)
        half = _gauge(0.30, now, elapsed=0.50)
        self.assertEqual(half.pace_cap(now, 2.0), cost.PACE_CEILING)


class IntegratedReleaseTest(unittest.TestCase):
    """#1112 : #777 conserve sa marge et libère au rythme du profil #1075."""

    def test_reserve_floor_survives_multiplier(self):
        now = 1_800_000_000.0
        for multiplier in (0.5, 1.0, 2.0):
            gauge = _gauge(0.09, now, elapsed=0.01)
            self.assertAlmostEqual(gauge.held_cap(now, 0.1, multiplier), 0.1)
            self.assertFalse(gauge.exceeded(now, 0.1, multiplier))

    def test_release_matches_threshold_for_every_multiplier(self):
        now = 1_800_000_000.0
        for multiplier in (0.5, 1.0, 2.0):
            for reserve in (0.0, 0.1):
                for used in (0.1, 0.25, 0.7):
                    gauge = _gauge(used, now, elapsed=0.01)
                    free = gauge.free_at(now, reserve, multiplier)
                    self.assertTrue(gauge.exceeded(free - 1, reserve, multiplier))
                    self.assertFalse(gauge.exceeded(free + 1, reserve, multiplier))

    def test_ceiling_still_waits_for_reset(self):
        now = 1_800_000_000.0
        gauge = _gauge(0.81, now, elapsed=0.1)
        self.assertEqual(gauge.free_at(now, 0.1, 2.0), gauge.resets_at)

    def test_accounts_release_uses_profile_multiplier(self):
        now = 1_800_000_000.0
        profile = accounts.parse({"codex": [{"name": "primaire", "pace_multiplier": 2}]})["codex"][0]
        gauge = _gauge(0.25, now, elapsed=0.01)
        free, held = accounts._release(profile, [gauge], [], now, 0.1)
        self.assertIs(held, gauge)
        self.assertAlmostEqual(free, now + (0.125 - 0.01) * gauge.window_s)


class LimitsTest(unittest.TestCase):
    """`accounts.limits` : compte 1 vs compte 2, plafonds fixes, retrait."""

    def _items(self, **settings):
        raw = [{"name": "primaire"}, {"name": "secondaire", "path": "/x/codex-2"}]
        for item in raw:
            item.update(settings.get(item["name"], {}))
        return accounts.parse({"codex": raw})["codex"]

    def assertCap(self, got, cap, label):
        self.assertEqual(got[1], label)
        self.assertAlmostEqual(got[0], cap, places=9)

    def test_primary_doubles_the_pace_secondary_does_not(self):
        now = 1_800_000_000.0
        primary, secondary = self._items(primaire={"pace_multiplier": 2.0})
        # non critique (réserve) : 2 × 10 % + 10 − 10 = 20 % contre 10 %
        self.assertCap(accounts.limits(primary, [_gauge(0.15, now, 0.10)], now,
                                       reserve=True)["codex-300min"], 0.20, "pace cap")
        self.assertCap(accounts.limits(secondary, [_gauge(0.15, now, 0.10)], now,
                                       reserve=True)["codex-300min"], 0.10, "pace cap")
        # critique (sans réserve) : 30 % contre 20 %
        self.assertCap(accounts.limits(primary, [_gauge(0.15, now, 0.10)], now,
                                       critical=True)["codex-300min"], 0.30, "pace cap")
        self.assertCap(accounts.limits(secondary, [_gauge(0.15, now, 0.10)], now,
                                       critical=True)["codex-300min"], 0.20, "pace cap")

    def test_fixed_cap_still_binds(self):
        now = 1_800_000_000.0
        primary, _ = self._items(primaire={"pace_multiplier": 2.0})
        # 50 % écoulés : le rythme multiplié (90 %) ne passe pas le plafond
        # fixe du compte par défaut (50 %), réservé aux agents non critiques.
        self.assertCap(accounts.limits(primary, [_gauge(0.40, now, 0.50)], now,
                                       reserve=True)["codex-300min"],
                       accounts.PRIMARY_CAP, "primary cap")
        # un `cap` déclaré garde aussi le dernier mot
        tight = accounts.parse({"codex": [{"name": "primaire", "cap": 0.25,
                                           "pace_multiplier": 2.0}]})["codex"][0]
        self.assertCap(accounts.limits(tight, [_gauge(0.10, now, 0.10)], now,
                                       critical=True)["codex-300min"], 0.25, "account cap")

    def test_removal_returns_to_nominal(self):
        now = 1_800_000_000.0
        gauge = _gauge(0.15, now, 0.10)
        removed = accounts.parse({"codex": [{"name": "primaire",
                                             "pace_multiplier": 1.0}]})["codex"][0]
        self.assertCap(accounts.limits(removed, [gauge], now,
                                       critical=True)["codex-300min"], 0.20, "pace cap")


class EvaluateTest(unittest.TestCase):
    """Le choix du compte lit le plafond multiplié du primaire, sans falsifier
    l'utilisation ni la part écoulée affichées."""

    def setUp(self):
        self.now = 1_800_000_000.0
        self.tmp = tempfile.mkdtemp(prefix="ameesh-1075-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        secondary_path = os.path.join(self.tmp, "codex-2")
        os.makedirs(secondary_path, mode=0o700)
        self.items = accounts.parse({"codex": [
            {"name": "primaire", "pace_multiplier": 2.0},
            {"name": "secondaire", "path": secondary_path, "check_credentials": False},
        ]})["codex"]
        self.reading = [_gauge(0.25, self.now, 0.10)]
        self.book = _Book({None: self.reading,
                           os.path.join(secondary_path, "sessions"): self.reading})

    def test_same_reading_holds_secondary_and_passes_primary(self):
        primary, secondary = self.items
        ev1 = accounts.evaluate(self.book, primary, self.items, self.now, critical=True)
        ev2 = accounts.evaluate(self.book, secondary, self.items, self.now, critical=True)
        self.assertTrue(ev1.ok, ev1.reason)
        self.assertFalse(ev2.ok)
        self.assertIn("25% used with 10% of the window elapsed (pace cap 20%)", ev2.reason)
        self.assertEqual(ev2.limits["codex-300min"][1], "pace cap")
        self.assertAlmostEqual(ev2.limits["codex-300min"][0], 0.20, places=9)

    def test_reason_shows_the_multiplied_cap_not_a_falsified_reading(self):
        primary = self.items[0]
        book = _Book({None: [_gauge(0.35, self.now, 0.10)]})
        ev = accounts.evaluate(book, primary, [primary], self.now, critical=True)
        self.assertFalse(ev.ok)
        # la part écoulée reste la vraie (10 %), seul le plafond vise plus haut
        self.assertIn("35% used with 10% of the window elapsed (pace cap 30%)", ev.reason)
        self.assertEqual(ev.held, ("codex-300min", 0.35))


if __name__ == "__main__":
    unittest.main()
