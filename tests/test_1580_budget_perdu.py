# SPDX-License-Identifier: AGPL-3.0-only
"""#1580 — the automatic choice no longer follows a fixed priority order.

Owner request (2026-10-15): "a choice score that favors the budget lost at
reset, on fresh readings only". Witnesses of the score
(`accounts.lost_at_reset`), of the criterion order of `rank` (budget criteria
before the declared order) and of the reason (`reason_of`).

The guards do not move: an account at the threshold stays out of the ranking,
and the declared order (L226b "primaries last") remains the tie-breaker at
equal budget.
"""
import unittest
from types import SimpleNamespace

from ameesh import accounts, cost

HOUR = 3600.0
WEEK = 7 * 24 * HOUR


def _profile(name, **settings):
    return accounts.Profile("claude", name, **settings)


def _gauge(used, resets_in, window=WEEK, key="seven_day"):
    now = 1_800_000_000.0
    return cost.Gauge("claude", key, used, now + resets_in, window)


class LostAtResetTest(unittest.TestCase):
    """`accounts.lost_at_reset`: the share lost at the next reset."""

    NOW = 1_800_000_000.0

    def test_closest_window(self):
        """It is the window that resets earliest that carries the lost share
        (`1 − used`), not the most used one."""
        gauges = [cost.Gauge("claude", "five_hour", 0.20, self.NOW + 2 * HOUR, 5 * HOUR),
                  cost.Gauge("claude", "seven_day", 0.60, self.NOW + 4 * 24 * HOUR, WEEK)]
        self.assertAlmostEqual(accounts.lost_at_reset(gauges, self.NOW), 0.80)

    def test_expired_reading_announces_no_loss(self):
        """A window reset since (expired reading) no longer expires: the score
        is 0, the reading is not counted as current (L71)."""
        echue = [cost.Gauge("claude", "seven_day", 0.95, self.NOW - 60, WEEK)]
        self.assertEqual(accounts.lost_at_reset(echue, self.NOW), 0.0)

    def test_without_reading_or_dated_window(self):
        self.assertEqual(accounts.lost_at_reset([], self.NOW), 0.0)
        sans_date = [cost.Gauge("claude", "seven_day", 0.50, None, WEEK)]
        self.assertEqual(accounts.lost_at_reset(sans_date, self.NOW), 0.0)

    def test_loss_per_second_until_reset(self):
        """#1789/N1: the lost share relative to the time remaining. A 5 h
        window at 20% that resets in 2 h loses 0.8 in 2 h, i.e. 0.4/h; the
        same share over 7 days evaporates 84 times slower."""
        court = [cost.Gauge("claude", "five_hour", 0.20, self.NOW + 2 * HOUR, 5 * HOUR)]
        long = [cost.Gauge("claude", "seven_day", 0.20, self.NOW + 2 * 24 * HOUR, WEEK)]
        self.assertAlmostEqual(accounts.lost_rate(court, self.NOW), 0.80 / (2 * HOUR))
        self.assertAlmostEqual(accounts.lost_rate(long, self.NOW), 0.80 / (2 * 24 * HOUR))
        self.assertGreater(accounts.lost_rate(court, self.NOW),
                           accounts.lost_rate(long, self.NOW))
        # nothing expires: no loss per second
        self.assertEqual(accounts.lost_rate([], self.NOW), 0.0)
        self.assertEqual(accounts.lost_rate(
            [cost.Gauge("claude", "seven_day", 0.95, self.NOW - 60, WEEK)], self.NOW), 0.0)
        self.assertEqual(accounts.lost_rate(
            [cost.Gauge("claude", "seven_day", 0.50, None, WEEK)], self.NOW), 0.0)


class OrdreDuChoixTest(unittest.TestCase):
    """`accounts.rank`: budget criteria before the declared order."""

    NOW = 1_800_000_000.0

    def _items(self):
        # declared order of the real case: tertiaire 10, secondaire 20, primaire 100
        return [_profile("primaire", priority=100), _profile("secondaire", priority=20),
                _profile("tertiaire", priority=10)]

    def _ev(self, name, gauges, limits=None, reason=""):
        return accounts.Evaluation(_profile(name), reason=reason, gauges=gauges,
                                   limits=limits or {})

    def test_most_behind_goes_first_even_declared_last(self):
        """The primaire is declared last (100) and is the least used: it goes
        before the tertiaire declared first (10)."""
        gauges = {
            "primaire": [_gauge(0.05, 3 * 24 * HOUR)],
            "secondaire": [_gauge(0.40, 3 * 24 * HOUR)],
            "tertiaire": [_gauge(0.80, 3 * 24 * HOUR)],
        }
        items = self._items()
        evs = {p.name: self._ev(p.name, gauges[p.name]) for p in items}
        self.assertEqual([p.name for p in accounts.rank(items, evs, self.NOW)],
                         ["primaire", "secondaire", "tertiaire"])

    def test_lost_share_breaks_tie_at_equal_pace_and_deadline(self):
        """Same lag, same deadline: the largest share lost at reset goes
        before the declared order (#1580)."""
        # explicit caps: 0.25/0.50 = 0.30/0.60 = 0.5 pace
        primaire = [_gauge(0.25, 3 * 24 * HOUR)]
        tertiaire = [_gauge(0.30, 3 * 24 * HOUR)]
        items = self._items()
        evs = {
            "primaire": self._ev("primaire", primaire,
                                 limits={"seven_day": (0.50, "primary cap")}),
            "secondaire": self._ev("secondaire", [_gauge(0.60, 3 * 24 * HOUR)],
                                   limits={"seven_day": (0.90, "pace cap")}),
            "tertiaire": self._ev("tertiaire", tertiaire,
                                  limits={"seven_day": (0.60, "pace cap")}),
        }
        # secondaire: 0.60/0.90 = 0.667 pace, it stays behind the two
        # others (0.5); between them, the loss per second decides: 0.75/3 d >
        # 0.70/3 d (the lost share breaks ties at equal loss)
        self.assertEqual(accounts.lost_at_reset(primaire, self.NOW), 0.75)
        self.assertEqual(accounts.lost_at_reset(tertiaire, self.NOW), 0.70)
        ranked = accounts.rank(items, evs, self.NOW)
        self.assertEqual([p.name for p in ranked], ["primaire", "tertiaire", "secondaire"])

    def test_fastest_loss_goes_before_lag(self):
        """#1789/N1: between two usable accounts, the one whose budget
        evaporates fastest per second goes before the one most behind on
        its pace."""
        # primaire: 5 h window at 10%, resets in 30 min → 0.9
        # lost in 0.5 h, i.e. 1.8/h; tertiaire: week intact, resets
        # in 5 days → 1.0 lost in 120 h, i.e. ~0.008/h.
        evs = {
            "primaire": self._ev("primaire", [_gauge(0.10, 0.5 * HOUR,
                                                     window=5 * HOUR,
                                                     key="five_hour")]),
            "tertiaire": self._ev("tertiaire", [_gauge(0.0, 5 * 24 * HOUR)]),
            "secondaire": self._ev("secondaire", [_gauge(0.50, 3 * 24 * HOUR)]),
        }
        self.assertGreater(accounts.lost_rate(evs["primaire"].gauges, self.NOW),
                           accounts.lost_rate(evs["tertiaire"].gauges, self.NOW))
        # tertiaire is the most behind (0%), but loses nothing: the
        # primaire goes first, the tertiaire before the secondaire (0.5
        # pace and 0.0069/h)
        self.assertEqual([p.name for p in accounts.rank(self._items(), evs, self.NOW)],
                         ["primaire", "tertiaire", "secondaire"])
        # the reason states the criterion that decided, not the lag (#1789/N1)
        self.assertIn("losing its budget fastest", accounts.reason_of(
            accounts.rank(self._items(), evs, self.NOW), evs, self.NOW))

    def test_at_equal_budget_the_declared_order_breaks_the_tie(self):
        """Identical budget: L226b remains the tie-breaker, and the reason
        says so."""
        items = self._items()
        evs = {p.name: self._ev(p.name, [_gauge(0.50, 3 * 24 * HOUR)]) for p in items}
        ranked = accounts.rank(items, evs, self.NOW)
        self.assertEqual([p.name for p in ranked], ["tertiaire", "secondaire", "primaire"])
        self.assertIn("first in the failover order",
                      accounts.reason_of(ranked, evs, self.NOW))

    def test_forecast_uses_the_same_caps_as_the_choice(self):
        """The primary's fixed cap reverses the order of the raw usage ratios."""
        cfg = SimpleNamespace(host="test", accounts={"claude": [
            {"name": "primaire", "cap": 0.50, "priority": 100},
            {"name": "tertiaire", "cap": 0.90, "priority": 10},
        ]})
        # #1789/N1: the loss rates are tied on purpose — 0.80 / (0.1 W) =
        # 0.70 / (0.0875 W) — so the fixed caps decide through `lag` alone:
        # primary .20/.50 = .40, tertiary .30/.90 ≈ .333. Without the caps
        # both read .20/pace and .30/pace, and the primary comes first.
        gauges = {"primaire": [_gauge(0.20, 0.1 * WEEK)],
                  "tertiaire": [_gauge(0.30, 0.0875 * WEEK)]}
        evaluations = {}

        def evaluate(book, profile, items, now, **kwargs):
            result = accounts.Evaluation(
                profile, gauges=gauges[profile.name],
                limits=accounts.limits(profile, gauges[profile.name], now))
            evaluations[profile.name] = result
            return result

        rows = accounts.report(cfg, None, None, now=self.NOW, evaluate_fn=evaluate,
                               record=False)
        items = accounts.parse(cfg.accounts)["claude"]
        ranked = accounts.rank(items, evaluations, self.NOW)
        # Actual ratios: primary .20/.50=.40, tertiary .30/.90=.333.
        # Without the fixed caps, the forecast incorrectly picks primary.
        self.assertEqual(ranked[0].name, "tertiaire")
        self.assertEqual([row["account"] for row in rows if row["next"]],
                         [ranked[0].name])
        self.assertEqual(next(row["why"] for row in rows if row["next"]),
                         accounts.reason_of(ranked, evaluations, self.NOW))

    def test_account_at_threshold_stays_out_of_the_ranking(self):
        """No overage: the score never resurrects an account set aside by the
        guard, even if it has the largest lost budget."""
        primaire = [_gauge(0.95, 3 * 24 * HOUR)]
        items = self._items()
        evs = {
            "primaire": self._ev("primaire", primaire,
                                 reason="seven_day 95% used (primary cap 50%)"),
            "secondaire": self._ev("secondaire", [_gauge(0.40, 3 * 24 * HOUR)]),
            "tertiaire": self._ev("tertiaire", [_gauge(0.60, 3 * 24 * HOUR)]),
        }
        self.assertEqual([p.name for p in accounts.rank(items, evs, self.NOW)],
                         ["secondaire", "tertiaire"])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
