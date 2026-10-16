# SPDX-License-Identifier: AGPL-3.0-only
"""#1176 — plafond de rythme par compte et relèvement de fin de fenêtre.

Décision de l'owner du 2026-10-14 (bureau, courrier 28505) : ne plus perdre la
fin des fenêtres de forfait. `pace_ceiling` remplace les 90 % en dur ;
`pace_ceiling_end` relève le plafond dans les dernières heures avant
`resets_at`, **seulement** quand l'overage est impossible, prouvé par un relevé
frais de jauges (capacité explicitement fausse). Le refus d'overage et la
coupure d'un tour sur `isUsingOverage` restent inchangés.

Aucun compte réel, aucune base : les jauges sont des objets `cost.Gauge`, les
dossiers de compte sont des dossiers du bac à sable.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import unittest

from ameesh import accounts, cost

HOUR = 3600.0
DAY = 24 * HOUR
WEEK = 7 * DAY
NOW = 1_800_000_000.0


def _gauge(used: float, *, before_reset: float = 3 * HOUR, key: str = "seven_day",
           window: float = WEEK, harness: str = "claude") -> cost.Gauge:
    """Une jauge dont la remise à zéro est dans `before_reset` secondes."""
    return cost.Gauge(harness, key, used, NOW + before_reset, window)


def _reading(gauges, overage=None) -> cost.Gauges:
    return cost.Gauges(list(gauges), overage)


class _Book:
    """Des jauges données, sans base ni écriture."""

    db = None

    def __init__(self, claude=None, codex=None):
        self._claude = claude if claude is not None else []
        self._codex = codex or {}

    def claude_gauges(self, account=None, primary=None):
        return self._claude

    def codex_gauges(self, sessions=None):
        return self._codex.get(sessions, [])

    def record_gauges(self, gauges, account=None):
        return 0


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-1176-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _dir(self, name: str) -> str:
        path = os.path.join(self.tmp, name)
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
        return path

    def _profile(self, harness: str = "claude", path: str | None = None,
                 name: str | None = None, **settings):
        raw = {"name": name or ("primaire" if path is None else "secondaire"),
               "check_credentials": False}
        if path is not None:
            raw["path"] = path
        raw.update(settings)
        return accounts.parse({harness: [raw]})[harness][0]

    def _claude_config(self, directory: str, reason, *, mtime: float | None = None,
                       text: str | None = None) -> str:
        path = os.path.join(directory, ".claude.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text if text is not None else json.dumps(
                {"cachedExtraUsageDisabledReason": reason}))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


class SettingsTest(unittest.TestCase):
    """Les réglages sont propres au compte, sans défaut implicite."""

    def _parse(self, **settings):
        raw = {"name": "secondaire", "path": "/x/compte"}
        raw.update(settings)
        return accounts.parse({"codex": [raw]})["codex"][0]

    def test_defaults_keep_the_guard_ceiling(self):
        profile = self._parse()
        self.assertEqual(profile.pace_ceiling, cost.PACE_CEILING)
        self.assertIsNone(profile.pace_ceiling_end)
        self.assertEqual(profile.pace_ceiling_end_hours, accounts.PACE_CEILING_END_HOURS)

    def test_settings_parsed_per_account(self):
        profile = self._parse(pace_ceiling=0.8, pace_ceiling_end=0.99,
                              pace_ceiling_end_hours=2)
        self.assertEqual((profile.pace_ceiling, profile.pace_ceiling_end,
                          profile.pace_ceiling_end_hours), (0.8, 0.99, 2.0))
        # un `pace_ceiling_end` sans heures prend la fenêtre par défaut (6 h)
        other = self._parse(pace_ceiling_end=0.95)
        self.assertEqual(other.pace_ceiling_end_hours, accounts.PACE_CEILING_END_HOURS)

    def test_invalid_settings_refused(self):
        for bad in ({"pace_ceiling": 0}, {"pace_ceiling": -0.5}, {"pace_ceiling": 1.5},
                    {"pace_ceiling": "x"}, {"pace_ceiling": True},
                    {"pace_ceiling": math.inf}, {"pace_ceiling": math.nan},
                    {"pace_ceiling_end": 0.5, "pace_ceiling": 0.8},   # end < ceiling
                    {"pace_ceiling_end_hours": 3},                    # heures sans fin
                    {"pace_ceiling_end": 0.99, "pace_ceiling_end_hours": 0},
                    {"pace_ceiling_end": 0.99, "pace_ceiling_end_hours": -1},
                    {"pace_ceiling_end": 0.99, "pace_ceiling_end_hours": "x"},
                    {"pace_ceiling_end": 0.99, "pace_ceiling_end_hours": True},
                    {"pace_ceiling_end": 0.99, "pace_ceiling_end_hours": math.inf}):
            with self.assertRaises(accounts.AccountError, msg=bad):
                self._parse(**bad)

    def test_end_equal_to_ceiling_is_a_no_op(self):
        profile = self._parse(pace_ceiling=0.9, pace_ceiling_end=0.9)
        gauge = _gauge(0.95, before_reset=HOUR)
        self.assertEqual(accounts.pace_ceiling(profile, _reading([gauge]), gauge, NOW),
                         accounts.Ceiling(0.9))


class GaugeMathTest(unittest.TestCase):
    """`Gauge` : le plafond transmis ne touche ni l'écoulé ni l'utilisation."""

    def test_pace_cap_uses_the_given_ceiling(self):
        gauge = _gauge(0.0, before_reset=0.05 * WEEK)             # écoulé 95 %
        self.assertAlmostEqual(gauge.pace_cap(NOW), cost.PACE_CEILING)
        self.assertAlmostEqual(gauge.pace_cap(NOW, 1.0, 0.98), 0.98)
        self.assertAlmostEqual(gauge.elapsed(NOW), 0.95)
        self.assertAlmostEqual(gauge.pace_cap(NOW, 1.0, 0.5), 0.5)

    def test_free_at_respects_the_ceiling(self):
        gauge = _gauge(0.96, before_reset=0.05 * WEEK)            # écoulé 95 %
        self.assertEqual(gauge.free_at(NOW, 0.0, 1.0, cost.PACE_CEILING),
                         gauge.resets_at)
        # relevé à 98 % : l'écoulé rattrape 96 % avant la remise à zéro
        free = gauge.free_at(NOW, 0.0, 1.0, 0.98)
        self.assertLess(free, gauge.resets_at)
        self.assertAlmostEqual(free, gauge.resets_at - gauge.window_s * (1.0 - 0.86))


class OverageProofTest(_Sandbox):
    """La preuve d'overage impossible : un relevé de jauges frais, rien d'autre.

    Revue Codex B2 (#1176) : le `.claude.json` du compte n'est plus une
    source de preuve (son cache peut dater d'une autre fenêtre, et le mtime du
    contenant ne date pas la valeur). Les témoins qui l'utilisaient gardent le
    plafond de base, y compris après réécriture du fichier dans la fenêtre.
    """

    def test_gauges_flag_not_capable_with_a_live_window_is_a_proof(self):
        gauge = _gauge(0.5, harness="codex", key="codex-300min", window=5 * HOUR)
        profile = self._profile("codex")
        self.assertTrue(accounts.overage_impossible(
            _reading([gauge], cost.Overage(capable=False)), NOW))

    def test_capable_unknown_or_possible_is_never_a_proof(self):
        gauge = _gauge(0.5, harness="codex", key="codex-300min", window=5 * HOUR)
        for overage in (cost.Overage(), cost.Overage(capable=True), cost.Overage(capable=None)):
            self.assertFalse(accounts.overage_impossible(
                _reading([gauge], overage), NOW), overage)

    def test_a_stale_reading_proves_nothing(self):
        gauge = _gauge(0.5, before_reset=-100.0, harness="codex",
                       key="codex-300min", window=5 * HOUR)
        self.assertFalse(accounts.overage_impossible(
            _reading([gauge], cost.Overage(capable=False)), NOW))

    def test_in_overage_is_never_impossible(self):
        gauge = _gauge(1.0, harness="codex", key="codex-300min", window=5 * HOUR)
        reading = _reading([gauge], cost.Overage(capable=False, in_use=True))
        self.assertTrue(reading.in_overage(NOW))
        self.assertFalse(accounts.overage_impossible(reading, NOW))

    def test_a_declared_overage_none_is_not_a_proof(self):
        gauge = _gauge(0.5, harness="codex", key="codex-300min", window=5 * HOUR)
        profile = self._profile("codex", overage="none", pace_ceiling=0.90,
                                pace_ceiling_end=0.98)
        reading = _reading([gauge])
        self.assertFalse(accounts.overage_impossible(reading, NOW))
        self.assertEqual(accounts.pace_ceiling(profile, reading, gauge, NOW).value, 0.90)

    def test_a_claude_cache_in_the_window_never_raises_the_ceiling(self):
        directory = self._dir("claude-2")
        self._claude_config(directory, "out_of_credits", mtime=NOW - 60.0)
        gauge = _gauge(0.95)
        profile = self._profile(path=directory, pace_ceiling=0.90, pace_ceiling_end=0.98)
        reading = _reading([gauge])              # capacité inconnue
        self.assertFalse(accounts.overage_impossible(reading, NOW))
        self.assertEqual(accounts.pace_ceiling(profile, reading, gauge, NOW).value, 0.90)

    def test_a_foreign_key_rewrite_is_not_a_fresh_proof(self):
        """Témoin de la revue B2 : réécrire le contenant ne date pas la valeur."""
        directory = self._dir("claude-2")
        path = self._claude_config(directory, "out_of_credits", mtime=NOW - 60.0)
        gauge = _gauge(0.95)
        profile = self._profile(path=directory, pace_ceiling=0.90, pace_ceiling_end=0.98)
        reading = _reading([gauge])
        self.assertEqual(accounts.pace_ceiling(profile, reading, gauge, NOW).value, 0.90)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["uneCleEtrangere"] = True            # la raison mise en cache est inchangée
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data))
        os.utime(path, (NOW - 30.0, NOW - 30.0))  # mtime dans la fenêtre
        self.assertEqual(accounts.pace_ceiling(profile, reading, gauge, NOW).value, 0.90)
        self.assertFalse(accounts.overage_impossible(reading, NOW))


class CodexCreditsTest(_Sandbox):
    """Revue Codex B1 (#1176) : du payload brut jusqu'à `limits`/`evaluate`.

    Un objet `credits` vide, partiel ou aux valeurs non booléennes ne prouve
    pas l'incapacité : `capable` reste None, et le plafond de fin de fenêtre
    ne se relève pas.
    """

    def _setup(self, credits, used: float = 95.0):
        home = self._dir("codex-home")
        sessions = os.path.join(home, "sessions")
        os.makedirs(sessions, exist_ok=True)
        with open(os.path.join(sessions, "fil.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"payload": {"rate_limits": {
                "primary": {"used_percent": used, "window_minutes": 10080,
                            "resets_at": NOW + 3 * HOUR},
                "credits": credits}}}) + "\n")
        self.profile = accounts.parse({"codex": [{
            "name": "secondaire", "path": home, "check_credentials": False,
            "pace_ceiling": 0.90, "pace_ceiling_end": 0.98}]})["codex"][0]
        return cost.CostBook(state_dir=self.tmp, codex_sessions=sessions)

    def test_empty_partial_or_non_boolean_credits_stay_unknown(self):
        for credits in ({}, {"balance": 12}, {"has_credits": "false"},
                        {"unlimited": "yes"}, {"has_credits": None}, {"unlimited": False}):
            reading = self._setup(credits).codex_gauges()
            self.assertEqual(reading.overage.capable, None, credits)
            self.assertFalse(reading.overage.in_use, credits)

    def test_explicit_capability_or_incapacity_is_reported(self):
        for credits, expected in (({"has_credits": True}, True),
                                  ({"unlimited": True}, True),
                                  ({"has_credits": False}, False),
                                  ({"has_credits": False, "unlimited": True}, True)):
            reading = self._setup(credits).codex_gauges()
            self.assertIs(reading.overage.capable, expected, credits)

    def test_from_the_raw_payload_to_the_ceiling_and_the_limits(self):
        for credits in ({}, {"balance": 12}, {"has_credits": "false"}):
            book = self._setup(credits)
            reading = book.codex_gauges(sessions=os.path.join(self.profile.path, "sessions"))
            gauge = reading[0]
            self.assertEqual(accounts.pace_ceiling(self.profile, reading, gauge, NOW).value,
                             0.90, credits)
            self.assertEqual(accounts.limits(self.profile, reading, NOW)[gauge.key],
                             (0.90, "pace cap"), credits)
            ev = accounts.evaluate(book, self.profile, [self.profile], NOW, record=False)
            self.assertFalse(ev.ok, credits)
            self.assertAlmostEqual(ev.limits[gauge.key][0], 0.90, msg=credits)
            self.assertIn("pace cap 90%", ev.reason)

    def test_an_explicit_incapacity_still_lifts_the_ceiling(self):
        book = self._setup({"has_credits": False})
        reading = book.codex_gauges(sessions=os.path.join(self.profile.path, "sessions"))
        gauge = reading[0]
        self.assertIs(reading.overage.capable, False)
        self.assertEqual(accounts.pace_ceiling(self.profile, reading, gauge, NOW).value, 0.98)
        ev = accounts.evaluate(book, self.profile, [self.profile], NOW, record=False)
        self.assertTrue(ev.ok, ev.reason)
        self.assertAlmostEqual(ev.limits[gauge.key][0], 0.98)
        self.assertEqual(ev.ceilings[gauge.key].reason, accounts.CEILING_REASON_END)


class EndOfWindowTest(_Sandbox):
    """Le relèvement : dans la fenêtre, prouvé, sinon plafond de base."""

    def _settings(self, **kw):
        raw = {"name": "secondaire", "path": "/x/compte", "check_credentials": False,
               "pace_ceiling": 0.90, "pace_ceiling_end": 0.98}
        raw.update(kw)
        return accounts.parse({"codex": [raw]})["codex"][0]

    def _codex(self, used=0.95, before_reset=3 * HOUR, window=WEEK,
               capable=False, in_use=False):
        gauge = _gauge(used, before_reset=before_reset, key="codex-10080min",
                       window=window, harness="codex")
        return gauge, _reading([gauge], cost.Overage(capable, in_use))

    def test_raised_only_inside_the_window_and_with_a_proof(self):
        profile = self._settings()
        gauge, reading = self._codex()
        self.assertEqual(accounts.pace_ceiling(profile, reading, gauge, NOW),
                         accounts.Ceiling(0.98, accounts.CEILING_REASON_END,
                                          gauge.resets_at - 6 * HOUR, 0.98))

    def test_outside_the_window_the_base_ceiling_holds(self):
        profile = self._settings()
        gauge, reading = self._codex(before_reset=10 * HOUR)
        found = accounts.pace_ceiling(profile, reading, gauge, NOW)
        self.assertEqual(found.value, 0.90)
        self.assertEqual(found.reason, "")
        self.assertEqual(found.from_ts, gauge.resets_at - 6 * HOUR)

    def test_a_custom_window_is_honoured(self):
        profile = self._settings(pace_ceiling_end_hours=12)
        gauge, reading = self._codex(before_reset=10 * HOUR)
        self.assertEqual(accounts.pace_ceiling(profile, reading, gauge, NOW).value, 0.98)
        gauge, reading = self._codex(before_reset=13 * HOUR)
        self.assertEqual(accounts.pace_ceiling(profile, reading, gauge, NOW).value, 0.90)

    def test_without_a_proof_or_without_settings_the_base_holds(self):
        for profile, capable in ((self._settings(), True),
                                 (self._settings(), None),
                                 (self._settings(pace_ceiling_end=None), False),
                                 (self._settings(pace_ceiling_end=0.90), False)):
            gauge, reading = self._codex(capable=capable)
            found = accounts.pace_ceiling(profile, reading, gauge, NOW)
            self.assertEqual(found.value, 0.90, (profile.pace_ceiling_end, capable))
            self.assertEqual(found.reason, "")

    def test_an_undated_or_reset_window_is_never_raised(self):
        profile = self._settings()
        undated = cost.Gauge("codex", "codex-10080min", 0.95, None, WEEK)
        self.assertEqual(accounts.pace_ceiling(
            profile, _reading([undated], cost.Overage(False)), undated, NOW).value, 0.90)
        passed = _gauge(0.95, before_reset=-100.0, key="codex-10080min",
                        window=WEEK, harness="codex")
        self.assertEqual(accounts.pace_ceiling(
            profile, _reading([passed], cost.Overage(False)), passed, NOW).value, 0.90)

    def test_each_window_has_its_own_ceiling(self):
        profile = self._settings()
        short = _gauge(0.95, before_reset=10 * HOUR, key="five_hour",
                       window=5 * HOUR, harness="codex")
        week = _gauge(0.95, before_reset=3 * HOUR, key="seven_day",
                      window=WEEK, harness="codex")
        ceilings = accounts.pace_ceilings(profile, _reading([short, week],
                                                            cost.Overage(False)), NOW)
        self.assertEqual(ceilings["five_hour"].value, 0.90)
        self.assertEqual(ceilings["seven_day"].value, 0.98)

    def test_two_accounts_keep_their_own_ceilings(self):
        simple, generous = self._settings(), self._settings(pace_ceiling=0.95,
                                                            pace_ceiling_end=0.99)
        gauge, reading = self._codex()
        self.assertEqual(accounts.pace_ceiling(simple, reading, gauge, NOW).value, 0.98)
        self.assertEqual(accounts.pace_ceiling(generous, reading, gauge, NOW).value, 0.99)
        # sans preuve, chacun garde SON plafond de base
        gauge, reading = self._codex(capable=True)
        self.assertEqual(accounts.pace_ceiling(simple, reading, gauge, NOW).value, 0.90)
        self.assertEqual(accounts.pace_ceiling(generous, reading, gauge, NOW).value, 0.95)


class LimitsAndEvaluationTest(_Sandbox):
    """`limits` et `evaluate` lisent le plafond effectif, sans toucher à l'overage."""

    def _setup(self, **settings):
        self.directory = self._dir("codex-2")
        raw = {"name": "secondaire", "path": self.directory, "check_credentials": False,
               "pace_ceiling": 0.90, "pace_ceiling_end": 0.98}
        raw.update(settings)
        self.profile = accounts.parse({"codex": [raw]})["codex"][0]
        self.items = [self.profile]

    def _book(self, gauge, capable=False, in_use=False):
        return _Book(codex={os.path.join(self.directory, "sessions"):
                            _reading([gauge], cost.Overage(capable, in_use))})

    def test_limits_use_the_raised_ceiling_inside_the_window(self):
        self._setup()
        gauge = _gauge(0.95, before_reset=3 * HOUR, key="codex-10080min",
                       window=WEEK, harness="codex")
        book = self._book(gauge)
        ev = accounts.evaluate(book, self.profile, self.items, NOW, critical=True)
        self.assertTrue(ev.ok, ev.reason)
        self.assertAlmostEqual(ev.limits["codex-10080min"][0], 0.98)
        self.assertEqual(ev.ceilings["codex-10080min"].reason, accounts.CEILING_REASON_END)

    def test_without_a_proof_the_account_stays_held(self):
        self._setup()
        gauge = _gauge(0.95, before_reset=3 * HOUR, key="codex-10080min",
                       window=WEEK, harness="codex")
        book = self._book(gauge, capable=None)
        ev = accounts.evaluate(book, self.profile, self.items, NOW, critical=True)
        self.assertFalse(ev.ok)
        self.assertAlmostEqual(ev.limits["codex-10080min"][0], 0.90)
        self.assertIn("pace cap 90%", ev.reason)

    def test_a_possible_overage_keeps_its_80_percent_cap(self):
        self._setup()
        gauge = _gauge(0.95, before_reset=3 * HOUR, key="codex-10080min",
                       window=WEEK, harness="codex")
        book = self._book(gauge, capable=True)
        ev = accounts.evaluate(book, self.profile, self.items, NOW, critical=True)
        self.assertFalse(ev.ok)
        self.assertAlmostEqual(ev.limits["codex-10080min"][0], accounts.OVERAGE_CAP)
        self.assertIn("overage cap 80%", ev.reason)

    def test_the_reserve_applies_to_the_raised_ceiling(self):
        self._setup()
        gauge = _gauge(0.95, before_reset=3 * HOUR, key="codex-10080min",
                       window=WEEK, harness="codex")
        ev = accounts.evaluate(self._book(gauge), self.profile, self.items, NOW,
                               reserve=True)
        self.assertFalse(ev.ok)
        self.assertAlmostEqual(ev.limits["codex-10080min"][0], 0.88)
        self.assertIn("10 points kept for critical agents", ev.reason)
        self.assertIn(accounts.CEILING_REASON_END, ev.reason)

    def test_a_fixed_cap_still_binds(self):
        self._setup(cap=0.5)
        gauge = _gauge(0.95, before_reset=3 * HOUR, key="codex-10080min",
                       window=WEEK, harness="codex")
        ev = accounts.evaluate(self._book(gauge), self.profile, self.items, NOW,
                               critical=True)
        self.assertFalse(ev.ok)
        self.assertAlmostEqual(ev.limits["codex-10080min"][0], 0.5)
        self.assertEqual(ev.limits["codex-10080min"][1], "account cap")

    def test_the_pause_announces_the_raise_hour_not_the_reset(self):
        self._setup()
        gauge = _gauge(0.95, before_reset=10 * HOUR, key="codex-10080min",
                       window=WEEK, harness="codex")
        ev = accounts.evaluate(self._book(gauge), self.profile, self.items, NOW,
                               critical=True)
        self.assertFalse(ev.ok)
        self.assertAlmostEqual(ev.limits["codex-10080min"][0], 0.90)
        self.assertAlmostEqual(ev.until, gauge.resets_at - 6 * HOUR)

    def test_in_overage_is_untouched_and_never_raised(self):
        self._setup(cap=None)
        gauge = _gauge(1.0, before_reset=3 * HOUR, key="codex-10080min",
                       window=WEEK, harness="codex")
        book = self._book(gauge, capable=False, in_use=True)
        ev = accounts.evaluate(book, self.profile, self.items, NOW, critical=True)
        self.assertFalse(ev.ok)
        self.assertTrue(ev.hard)
        self.assertIn("in overage", ev.reason)
        self.assertEqual(accounts.pace_ceiling(
            self.profile, _reading([gauge], cost.Overage(False, True)), gauge, NOW).value,
            0.90)


class ReportTest(_Sandbox):
    """`accounts list` montre le plafond effectif et sa raison (#1176)."""

    def _report(self, capable):
        directory = self._dir("claude-2")
        cfg = type("Cfg", (), {"host": "h", "accounts": {"claude": [
            {"name": "secondaire", "path": directory, "check_credentials": False,
             "pace_ceiling": 0.90, "pace_ceiling_end": 0.98}]}})()
        gauge = _gauge(0.95, before_reset=3 * HOUR)
        book = _Book(claude=_reading([gauge], cost.Overage(capable)))
        return accounts.report(cfg, None, book, now=NOW, record=False)

    def test_rows_carry_the_effective_ceiling_and_its_reason(self):
        row = self._report(False)[0]
        self.assertEqual(row["gauges"][0]["pace_ceiling"], 0.98)
        self.assertEqual(row["gauges"][0]["ceiling_reason"], accounts.CEILING_REASON_END)
        self.assertTrue(row["ok"], row["reason"])
        self.assertIn("ceiling 98%%, %s" % accounts.CEILING_REASON_END,
                      accounts.format_rows([row]))

    def test_without_a_proof_the_row_keeps_the_base_ceiling(self):
        row = self._report(True)[0]
        self.assertEqual(row["gauges"][0]["pace_ceiling"], 0.90)
        self.assertEqual(row["gauges"][0]["ceiling_reason"], "")
        self.assertNotIn("ceiling", accounts.format_rows([row]))

    def test_the_next_choice_explains_with_the_effective_cap(self):
        row = self._report(False)[0]
        self.assertTrue(row["next"])
        self.assertIn("pace 98%", row["why"])


if __name__ == "__main__":
    unittest.main()
