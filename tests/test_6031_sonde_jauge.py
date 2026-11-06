# SPDX-License-Identifier: AGPL-3.0-only
"""#6031 : une pause budget relit la jauge Codex avant de se maintenir.

Le 06/11, la famille Codex est restée en pause d'environ 00:33Z à 09:05Z sur
un relevé à 100 % qui n'était plus la fenêtre courante : les agents en pause
ne font pas de tour, donc aucun relevé neuf n'était publié. Le correctif lit
la jauge chez le fournisseur **sans tour** (`account/rateLimits/read` de
`codex app-server`) dès que le relevé qui fonde la pause a plus d'une heure,
puis rejoue la décision sur ce relevé frais. L'alerte `budget_pause_long`
affiche l'âge du relevé et nomme un relevé ancien comme cause possible.

Aucun test ne lance le vrai `codex` : la sonde est remplacée, et la conversion
de la réponse est éprouvée sur une charge utile réelle.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from ameesh import accounts, cost, exploitation, gauge_probe, registry, storage

from .support import PgTestCase

WEEK = 7 * 86400.0
NOW = 1_800_000_000.0


def reading(used: float, *, resets_in: float = 3 * 86400,
            read_at: float | None = None, overage: cost.Overage | None = None,
            key: str = "codex-10080min") -> cost.Gauges:
    gauge = cost.Gauge("codex", key, used, NOW + resets_in, WEEK,
                       read_at=read_at, produced_at=read_at, source="test")
    return cost.Gauges([gauge], overage)


class ResponseTest(unittest.TestCase):
    """La réponse du fournisseur devient les mêmes fenêtres que le journal."""

    def test_primary_and_credits_are_converted(self):
        payload = {"rateLimits": {
            "primary": {"usedPercent": 100, "resetsAt": int(NOW + WEEK),
                        "windowDurationMins": 10080},
            "secondary": None,
            "credits": {"hasCredits": True, "unlimited": False},
        }}
        gauges = gauge_probe.gauges_from_response(payload, NOW)
        self.assertEqual([g.key for g in gauges], ["codex-10080min"])
        self.assertAlmostEqual(gauges[0].used, 1.0)
        self.assertEqual(gauges[0].resets_at, NOW + WEEK)
        self.assertEqual(gauges[0].window_s, WEEK)
        self.assertEqual(gauges[0].read_at, NOW)
        self.assertEqual(gauges[0].produced_at, NOW)
        # une fenêtre saturée avec des crédits est de l'overage possible
        self.assertTrue(gauges.overage.capable)
        self.assertTrue(gauges.overage.in_use)

    def test_two_windows_and_an_explicit_incapacity(self):
        payload = {"rateLimits": {
            "primary": {"usedPercent": 20, "resetsAt": int(NOW + 5 * 3600),
                        "windowDurationMins": 300},
            "secondary": {"usedPercent": 0, "resetsAt": int(NOW + WEEK),
                          "windowDurationMins": 10080},
            "credits": {"hasCredits": False, "unlimited": False},
        }}
        gauges = gauge_probe.gauges_from_response(payload, NOW)
        self.assertEqual([g.key for g in gauges],
                         ["codex-300min", "codex-10080min"])
        self.assertAlmostEqual(gauges[0].used, 0.20)
        self.assertFalse(gauges.overage.capable)
        self.assertFalse(gauges.overage.in_use)

    def test_the_multi_bucket_view_is_a_fallback(self):
        payload = {"rateLimitsByLimitId": {"codex": {
            "primary": {"usedPercent": 50, "resetsAt": int(NOW + WEEK),
                        "windowDurationMins": 10080}}}}
        gauges = gauge_probe.gauges_from_response(payload, NOW)
        self.assertAlmostEqual(gauges[0].used, 0.50)

    def test_a_response_without_a_window_proves_nothing(self):
        self.assertIsNone(gauge_probe.gauges_from_response({"rateLimits": {}}, NOW))
        self.assertIsNone(gauge_probe.gauges_from_response({}, NOW))

    def test_ordinary_usage_not_allowed_does_not_lift_a_pause(self):
        payload = {"ordinaryUsageAllowed": False, "rateLimits": {
            "primary": {"usedPercent": 0, "resetsAt": int(NOW + WEEK),
                        "windowDurationMins": 10080}}}
        self.assertIsNone(gauge_probe.gauges_from_response(payload, NOW))
        payload["ordinaryUsageAllowed"] = True
        self.assertIsNotNone(gauge_probe.gauges_from_response(payload, NOW))


class MergeTest(unittest.TestCase):
    """La réponse ne remplace que les fenêtres qu'elle publie."""

    def test_a_window_absent_from_the_response_is_kept(self):
        short = cost.Gauge("codex", "codex-300min", 1.0, NOW + 1800, 5 * 3600,
                           read_at=NOW - 2 * 3600, produced_at=NOW - 2 * 3600)
        week = cost.Gauge("codex", "codex-10080min", 1.0, NOW + 3 * 86400, WEEK,
                          read_at=NOW - 2 * 3600, produced_at=NOW - 2 * 3600)
        local = cost.Gauges([short, week], cost.Overage(True, True))
        fresh = cost.Gauges([cost.Gauge("codex", "codex-10080min", 0.01,
                                        NOW + WEEK, WEEK, read_at=NOW,
                                        produced_at=NOW)],
                            cost.Overage(True, False))
        merged = gauge_probe.merge(local, fresh, now=NOW)
        by_key = {g.key: g for g in merged}
        self.assertIs(by_key["codex-300min"], short)
        self.assertAlmostEqual(by_key["codex-10080min"].used, 0.01)
        # une courte encore saturée garde l'overage sur l'union
        self.assertTrue(merged.overage.in_use)

    def test_provider_credit_capability_replaces_the_local_value(self):
        for previous, current in ((True, False), (False, True)):
            with self.subTest(previous=previous, current=current):
                local = reading(1.0, read_at=NOW - 7200,
                                overage=cost.Overage(previous, previous))
                fresh = reading(1.0, read_at=NOW,
                                overage=cost.Overage(current, current))
                merged = gauge_probe.merge(local, fresh, now=NOW)
                self.assertIs(merged.overage.capable, current)
                self.assertIs(merged.overage.in_use, current)

    def test_a_cleared_weekly_no_longer_carries_the_overage(self):
        local = cost.Gauges([cost.Gauge("codex", "codex-10080min", 1.0,
                                        NOW + 3 * 86400, WEEK, read_at=NOW - 2 * 3600,
                                        produced_at=NOW - 2 * 3600)],
                            cost.Overage(True, True))
        fresh = cost.Gauges([cost.Gauge("codex", "codex-10080min", 0.01,
                                        NOW + WEEK, WEEK, read_at=NOW,
                                        produced_at=NOW)], cost.Overage(True, False))
        merged = gauge_probe.merge(local, fresh, now=NOW)
        self.assertFalse(merged.overage.in_use)


class ProbeProcessTest(unittest.TestCase):
    """La sonde parle vraiment à `codex app-server` (faux binaire local)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-probe-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "codex-home")
        os.makedirs(self.home)
        self.fake = os.path.join(self.tmp, "codex")
        with open(self.fake, "w", encoding="utf-8") as fh:
            fh.write("""#!/usr/bin/env python3
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method = message.get("method")
    if method == "initialize":
        version = (message.get("params") or {}).get("clientInfo", {}).get("version")
        if not version:
            send({"id": message["id"], "error": {"code": -32600,
                  "message": "Invalid request: missing field `version`"}})
            continue
        send({"id": message["id"], "result": {"userAgent": "fake"}})
    elif method == "account/rateLimits/read":
        send({"id": message["id"], "result": {"rateLimits": {
            "primary": {"usedPercent": 42, "resetsAt": 1800604800,
                        "windowDurationMins": 10080},
            "secondary": None,
            "credits": {"hasCredits": True, "unlimited": False}}}})
""")
        os.chmod(self.fake, 0o755)

    def test_a_full_read_returns_the_provider_windows(self):
        profile = accounts.Profile("codex", "primaire", kind="config_dir",
                                   path=self.home, declared=True,
                                   check_credentials=False)
        env = dict(os.environ)
        env["AMEESH_CODEX_BIN"] = self.fake
        got = gauge_probe.probe(profile, environ=env, now=NOW)
        self.assertIsNotNone(got)
        self.assertEqual([g.key for g in got], ["codex-10080min"])
        self.assertAlmostEqual(got[0].used, 0.42)
        self.assertEqual(got[0].resets_at, 1800604800)
        self.assertTrue(got.overage.capable)


class RefreshTest(unittest.TestCase):
    """La sonde ne part que sur un relevé daté, ancien, et une fois par délai."""

    def setUp(self):
        gauge_probe._ATTEMPTS.clear()
        self.addCleanup(gauge_probe._ATTEMPTS.clear)
        self.profile = accounts.Profile("codex", "primaire", kind="config_dir",
                                        path="/tmp/codex-probe", declared=True,
                                        check_credentials=False)
        self.fresh = reading(0.01, resets_in=WEEK, read_at=NOW)

    def _refresh(self, gauges, *, held="codex-10080min", clock=1000.0, now=NOW,
                 result=None):
        with mock.patch.object(gauge_probe, "probe", return_value=result) as probe:
            got = gauge_probe.refresh(self.profile, gauges, held=held, now=now,
                                      clock=lambda: clock)
        return got, probe

    def test_an_old_reading_is_probed(self):
        got, probe = self._refresh(reading(1.0, read_at=NOW - 2 * 3600),
                                   result=self.fresh)
        self.assertIs(got, self.fresh)
        probe.assert_called_once()

    def test_a_fresh_reading_is_not_probed(self):
        got, probe = self._refresh(reading(1.0, read_at=NOW - 600),
                                   result=self.fresh)
        self.assertIsNone(got)
        probe.assert_not_called()

    def test_an_undated_reading_is_not_probed(self):
        got, probe = self._refresh(reading(1.0, read_at=None), result=self.fresh)
        self.assertIsNone(got)
        probe.assert_not_called()

    def test_a_failed_attempt_is_not_repeated_before_the_retry_delay(self):
        old = reading(1.0, read_at=NOW - 2 * 3600)
        got, probe = self._refresh(old, result=None, clock=1000.0)
        self.assertIsNone(got)
        probe.assert_called_once()
        # même compte, quelques secondes plus tard : pas de nouvelle sonde
        got, probe = self._refresh(old, result=self.fresh, clock=1000.0 + 60)
        self.assertIsNone(got)
        probe.assert_not_called()
        # au-delà du délai de reprise, la sonde repart
        got, probe = self._refresh(old, result=self.fresh,
                                   clock=1000.0 + gauge_probe.PROBE_RETRY_S + 1)
        self.assertIs(got, self.fresh)
        probe.assert_called_once()

    def test_a_non_codex_harness_is_never_probed(self):
        profile = accounts.Profile("claude", "primaire", kind="config_dir",
                                   path="/tmp/claude-probe", declared=True,
                                   check_credentials=False)
        with mock.patch.object(gauge_probe, "probe", return_value=self.fresh) as probe:
            got = gauge_probe.refresh(profile, reading(1.0, read_at=NOW - 2 * 3600),
                                      held="codex-10080min", now=NOW)
        self.assertIsNone(got)
        probe.assert_not_called()


class _Book:
    """Le strict nécessaire d'un `CostBook` pour la décision de pause."""

    def __init__(self, db=None):
        self.db = db
        self.recorded = []

    def record_gauges(self, gauges, account=None):
        self.recorded.append((list(gauges), account))
        return len(gauges)


class EvaluateTest(PgTestCase):
    """`accounts.evaluate` rejoue la décision sur la jauge relue."""

    def setUp(self):
        super().setUp()
        gauge_probe._ATTEMPTS.clear()
        self.addCleanup(gauge_probe._ATTEMPTS.clear)
        home = os.path.join(self.tmp, "codex-home")
        os.makedirs(home, exist_ok=True)
        os.chmod(home, 0o700)
        self.profile = accounts.Profile("codex", "primaire", kind="config_dir",
                                        path=home, declared=True,
                                        check_credentials=False)
        self.book = _Book(self.db)
        self.fresh = reading(0.01, resets_in=WEEK, read_at=NOW,
                             overage=cost.Overage(True, False))

    def _evaluate(self, stale, *, probe=True, record=True):
        with mock.patch.object(accounts, "gauges_of", return_value=stale), \
                mock.patch.object(gauge_probe, "probe",
                                  return_value=self.fresh) as sonde:
            result = accounts.evaluate(self.book, self.profile, [self.profile],
                                       NOW, record=record, probe=probe)
        return result, sonde

    def test_a_stale_saturated_reading_is_refreshed_and_the_pause_lifts(self):
        stale = reading(1.0, read_at=NOW - 2 * 3600,
                        overage=cost.Overage(True, True))
        result, sonde = self._evaluate(stale)
        self.assertTrue(result.ok, result.reason)
        sonde.assert_called_once()
        # la lecture fraîche est publiée dans l'historique partagé
        self.assertTrue(self.book.recorded)
        published, account = self.book.recorded[-1]
        self.assertEqual(account, "primaire")
        self.assertAlmostEqual(published[0].used, 0.01)

    def test_a_short_window_absent_from_the_probe_still_holds(self):
        short = cost.Gauge("codex", "codex-300min", 1.0, NOW + 1800, 5 * 3600,
                           read_at=NOW - 2 * 3600, produced_at=NOW - 2 * 3600)
        week = cost.Gauge("codex", "codex-10080min", 1.0, NOW + 3 * 86400, WEEK,
                          read_at=NOW - 2 * 3600, produced_at=NOW - 2 * 3600)
        stale = cost.Gauges([short, week], cost.Overage(True, True))
        result, sonde = self._evaluate(stale)
        self.assertFalse(result.ok)
        sonde.assert_called_once()

    def test_the_overage_branch_is_refreshed_too(self):
        stale = reading(1.0, read_at=NOW - 2 * 3600,
                        overage=cost.Overage(True, True))
        result, sonde = self._evaluate(stale)
        self.assertFalse(result.hard)
        sonde.assert_called_once()

    def test_a_fresh_saturated_reading_keeps_the_pause_without_a_probe(self):
        stale = reading(1.0, read_at=NOW - 600,
                        overage=cost.Overage(True, True))
        result, sonde = self._evaluate(stale)
        self.assertFalse(result.ok)
        sonde.assert_not_called()

    def test_record_false_never_probes(self):
        stale = reading(1.0, read_at=NOW - 2 * 3600,
                        overage=cost.Overage(True, True))
        result, sonde = self._evaluate(stale, record=False)
        self.assertFalse(result.ok)
        sonde.assert_not_called()

    def test_probe_false_never_probes(self):
        stale = reading(1.0, read_at=NOW - 2 * 3600,
                        overage=cost.Overage(True, True))
        result, sonde = self._evaluate(stale, probe=False)
        self.assertFalse(result.ok)
        sonde.assert_not_called()


class WithheldTest(PgTestCase):
    """Un relevé partiel trop ancien est relu avant de figer la pause (#1672)."""

    def setUp(self):
        super().setUp()
        gauge_probe._ATTEMPTS.clear()
        self.addCleanup(gauge_probe._ATTEMPTS.clear)
        home = os.path.join(self.tmp, "codex-home")
        os.makedirs(home, exist_ok=True)
        os.chmod(home, 0o700)
        self.items = [accounts.Profile("codex", "primaire", kind="config_dir",
                                       path=home, declared=True,
                                       check_credentials=False)]
        self.book = _Book(self.db)

    def test_probe_false_never_refreshes_a_withheld_reading(self):
        now = time.time()
        partial = cost.Gauges([
            cost.Gauge("codex", "codex-300min", 1.0, now - 3600, 5 * 3600,
                       read_at=now - 3 * 3600),
            cost.Gauge("codex", "codex-10080min", 0.5, now + 1800, WEEK,
                       read_at=now - 3 * 3600),
        ])
        with mock.patch.object(accounts, "gauges_of", return_value=partial), \
                mock.patch.object(gauge_probe, "refresh") as refresh:
            choice = accounts.choose(self.db, self.cfg.host, "codex", self.items,
                                     self.book, now=now, cfg=self.cfg, probe=False)
        self.assertIsNone(choice.profile)
        refresh.assert_not_called()

    def test_a_stale_partial_reading_is_refreshed_then_placed(self):
        now = time.time()
        partial = cost.Gauges([
            # la courte est échue (0 % à now) ; la semaine est ouverte mais
            # son relevé (3 h) est plus vieux que ce qui lui reste (30 min)
            cost.Gauge("codex", "codex-300min", 1.0, now - 3600, 5 * 3600,
                       read_at=now - 3 * 3600),
            cost.Gauge("codex", "codex-10080min", 0.5, now + 1800, WEEK,
                       read_at=now - 3 * 3600),
        ])
        fresh = cost.Gauges([
            cost.Gauge("codex", "codex-10080min", 0.01, now + WEEK, WEEK,
                       read_at=now, produced_at=now, source="test"),
        ])
        with mock.patch.object(accounts, "gauges_of", return_value=partial), \
                mock.patch.object(gauge_probe, "probe", return_value=fresh) as sonde:
            choice = accounts.choose(self.db, self.cfg.host, "codex", self.items,
                                     self.book, now=now, cfg=self.cfg, probe=True)
        self.assertIsNotNone(choice.profile)
        self.assertEqual(choice.profile.name, "primaire")
        self.assertEqual(choice.reason, "")
        sonde.assert_called_once()


class AlertAgeTest(PgTestCase):
    """`budget_pause_long` dit l'âge du relevé et nomme la cause possible."""

    def setUp(self):
        super().setUp()
        self.register("ouvrier", "codex", cwd=self.tmp)
        # le schéma est partagé par la classe : aucun relevé d'un témoin précédent
        self.db.execute("DELETE FROM quota_gauge_readings")

    def _pause(self, texte, minutes):
        registry.set_status(self.db, "ouvrier", "blocked", status_text=texte,
                            error="budget")
        self.db.execute(
            "UPDATE agent_registry SET status_since = now() - make_interval(mins => %s)"
            " WHERE name = 'ouvrier'", (int(minutes),))

    def _alertes(self, now):
        return [a for a in exploitation.alerts(self.cfg, self.db, now=now)
                if a["type"] == "budget_pause_long"]

    def test_an_old_reading_is_shown_and_named(self):
        now = time.time()
        storage.of(self.db).operations.record_gauges([{
            "harness": "codex", "key": "codex-10080min", "used": 1.0,
            "resets_at": now + 3 * 86400, "window_s": WEEK,
            "produced_at": now - 2 * 3600, "source": "test",
            "account": "primaire",
        }])
        self._pause("budget : pause (account primaire, codex-10080min 100 %) "
                    "until 13/11 08:30; …", 120)
        alertes = self._alertes(now)
        self.assertEqual(len(alertes), 1)
        detail = alertes[0]["detail"]
        self.assertIn("last gauge reading", detail)
        self.assertIn("2 h", detail)
        self.assertIn("stale reading can hold the pause", detail)

    def test_a_recent_reading_is_shown_without_the_stale_cause(self):
        now = time.time()
        storage.of(self.db).operations.record_gauges([{
            "harness": "codex", "key": "codex-10080min", "used": 0.98,
            "resets_at": now + 3 * 86400, "window_s": WEEK,
            "produced_at": now - 300, "source": "test", "account": "primaire",
        }])
        self._pause("budget : pause (account primaire, codex-10080min 100 %) "
                    "until 13/11 08:30; …", 120)
        alertes = self._alertes(now)
        self.assertEqual(len(alertes), 1)
        detail = alertes[0]["detail"]
        self.assertIn("last gauge reading", detail)
        self.assertNotIn("stale reading can hold the pause", detail)

    def test_pause_account_reads_the_preambule(self):
        self.assertEqual(accounts.pause_account(
            "pause (account secondaire, five_hour 96 %) until 14:20; x"),
            "secondaire")
        self.assertEqual(accounts.pause_account(
            "critical agent: pause (account primaire, seven_day 99 %) until 14:20"),
            "primaire")
        self.assertIsNone(accounts.pause_account("account hourly cap: 2.00 $"))
        self.assertEqual(accounts.pause_gauge(
            "pause (account secondaire, five_hour 96 %) until 14:20; x"),
            "five_hour")
        self.assertEqual(accounts.pause_gauge(
            "critical agent: pause (account primaire, seven_day 99 %) until 14:20"),
            "seven_day")
        self.assertIsNone(accounts.pause_gauge("account hourly cap: 2.00 $"))

    def test_the_age_is_the_holding_gauge_not_a_fresher_one(self):
        now = time.time()
        storage.of(self.db).operations.record_gauges([
            {"harness": "codex", "key": "codex-10080min", "used": 0.5,
             "resets_at": now + 3 * 86400, "window_s": WEEK,
             "produced_at": now - 60, "source": "test", "account": "primaire"},
            {"harness": "codex", "key": "codex-300min", "used": 1.0,
             "resets_at": now + 1800, "window_s": 5 * 3600,
             "produced_at": now - 2 * 3600, "source": "test",
             "account": "primaire"},
        ])
        self._pause("budget : pause (account primaire, codex-300min 100 %) "
                    "until 13/11 08:30; …", 120)
        alertes = self._alertes(now)
        self.assertEqual(len(alertes), 1)
        self.assertIn("2 h", alertes[0]["detail"])


if __name__ == "__main__":
    unittest.main()
