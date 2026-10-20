# SPDX-License-Identifier: AGPL-3.0-only
"""#2843 — deux défauts non bloquants du chantier #2839.

1. **Vieillissement des fenêtres réconciliées.** `codex_gauges.reconcile`
   conserve la dernière observation de **chaque** fenêtre ; une fenêtre courte
   échue peut donc rester dans le relevé à côté d'une semaine récemment
   observée. L'âge du relevé se lit alors par fenêtre : l'ancienne date du
   court conservé ne doit pas vieillir artificiellement la semaine
   (`cost.reading_outdated`, `accounts._informative_partial`), sans relâcher
   la prudence quand la semaine elle-même est ancienne.

2. **Échantillon tronqué.** La vérification du format attendu ne doit pas se
   contenter d'une fenêtre courte **échue** : une courte saturée vivante peut
   être hors de l'échantillon borné de 32 journaux, et l'échue la masquerait
   (`codex_gauges.reconcile`, `truncated=True`).
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

from ameesh import accounts, codex_gauges, cost
from ameesh.codex_gauges import AmbiguousCodexGauges

from .test_cost import Sandbox, write

NOW = 1_800_000_000.0
HOUR = 3600.0
DAY = 86400.0
WEEK = 7 * DAY


def event(now, *, short=100, week=49, plan="team", short_reset=None,
          week_reset=None, short_absent=False):
    """Un événement `rate_limits` d'un journal Codex, daté à `now`."""
    primary = None
    if not short_absent:
        primary = {"used_percent": short, "window_minutes": 300,
                   "resets_at": now + HOUR if short_reset is None else short_reset}
    return {"timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "payload": {"rate_limits": {
                "plan_type": plan, "credits": {"has_credits": True},
                "primary": primary,
                "secondary": {"used_percent": week, "window_minutes": 10080,
                              "resets_at": now + DAY if week_reset is None
                              else week_reset}}}}


def journal(root, name, *events, mtime=None):
    path = os.path.join(root, name + ".jsonl")
    write(path, {"type": "session_meta", "payload": {"creator_account_id": "account-a"}},
          *events)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def gauge(key, used, resets_at, window_s, read_at):
    return cost.Gauge("codex", key, used, resets_at, window_s, read_at=read_at)


def evaluation(gauges, limits=None):
    return accounts.Evaluation(accounts.Profile("codex", "secondaire"),
                               gauges=gauges, limits=limits or {})


class PerWindowAgeTest(unittest.TestCase):
    """L'âge d'une fenêtre est celui de **son** observation (défaut 1)."""

    def _recent_week(self):
        # Le court a expiré il y a six jours et reste visible (valeur
        # historique) ; la semaine a été observée il y a une heure, à une
        # heure de sa remise à zéro.
        short = gauge("codex-300min", 1.0, NOW - 6 * DAY, 5 * HOUR, NOW - 6 * DAY)
        week = gauge("codex-10080min", 0.49, NOW + HOUR, WEEK, NOW - HOUR)
        return short, week

    def _old_week(self):
        short = gauge("codex-300min", 1.0, NOW - 6 * DAY, 5 * HOUR, NOW - 6 * DAY)
        week = gauge("codex-10080min", 0.49, NOW + HOUR, WEEK, NOW - 6 * DAY)
        return short, week

    def test_a_recent_week_stays_usable_beside_a_retained_expired_short(self):
        gauges = list(self._recent_week())
        self.assertTrue(cost.stale_reading(gauges, NOW))
        self.assertFalse(cost.reading_outdated(gauges, NOW))
        self.assertTrue(accounts.placeable(evaluation(gauges), NOW))

    def test_an_expired_short_does_not_rejuvenate_an_old_week(self):
        gauges = list(self._old_week())
        self.assertTrue(cost.stale_reading(gauges, NOW))
        self.assertTrue(cost.reading_outdated(gauges, NOW))
        self.assertFalse(accounts.placeable(evaluation(gauges), NOW))

    def test_the_informative_partial_uses_the_same_per_window_age(self):
        limits = {"codex-10080min": (0.60, "pace cap")}
        recent = list(self._recent_week())
        old = list(self._old_week())
        self.assertTrue(accounts._informative_partial(
            recent, evaluation(recent, limits), NOW))
        self.assertFalse(accounts._informative_partial(
            old, evaluation(old, limits), NOW))


class ReconciledReadingAgeTest(Sandbox, unittest.TestCase):
    """Le relevé réconcilié garde la date de **chaque** observation (défaut 1)."""

    def test_a_retained_short_does_not_age_the_recent_weekly_reading(self):
        week_reset = self.now + HOUR
        journal(self.codex, "old",
                event(self.now - 6 * DAY, short=100,
                      short_reset=self.now - 6 * DAY + 5 * HOUR,
                      week=90, week_reset=week_reset),
                mtime=self.now - 6 * DAY)
        journal(self.codex, "recent",
                event(self.now - HOUR, short_absent=True, week=49,
                      week_reset=week_reset),
                mtime=self.now - HOUR)
        gauges = self.book().codex_gauges()
        ages = {g.key: g.read_at for g in gauges}
        self.assertEqual(ages["codex-300min"], self.now - 6 * DAY)
        self.assertEqual(ages["codex-10080min"], self.now - HOUR)
        self.assertTrue(cost.stale_reading(gauges, self.now))
        self.assertFalse(cost.reading_outdated(gauges, self.now))
        self.assertTrue(accounts.placeable(evaluation(gauges), self.now))

    def test_a_weekly_reading_that_is_itself_old_stays_withheld(self):
        journal(self.codex, "old",
                event(self.now - 6 * DAY, short=100,
                      short_reset=self.now - 6 * DAY + 5 * HOUR,
                      week=49, week_reset=self.now + HOUR),
                mtime=self.now - 6 * DAY)
        gauges = self.book().codex_gauges()
        self.assertTrue(cost.stale_reading(gauges, self.now))
        self.assertTrue(cost.reading_outdated(gauges, self.now))
        self.assertFalse(accounts.placeable(evaluation(gauges), self.now))


class TruncatedSampleTest(Sandbox, unittest.TestCase):
    """Un échantillon tronqué exige une courte **vivante** (défaut 2)."""

    def _sample_with_expired_short(self):
        # La courte vivante à 100 % est la plus ancienne par mtime : les 32
        # journaux plus récents la masquent. L'un d'eux porte une courte
        # échue à 100 %, qui satisfaisait la vérification de présence.
        journal(self.codex, "hidden-live",
                event(self.now, short=100, week=49), mtime=self.now)
        for i in range(codex_gauges.MAX_FILES):
            if i == 0:
                journal(self.codex, "expired",
                        event(self.now, short=100, week=49,
                              short_reset=self.now - 60),
                        mtime=self.now + i + 1)
            else:
                journal(self.codex, "partial-%d" % i,
                        event(self.now, short_absent=True, week=49),
                        mtime=self.now + i + 1)

    def test_an_expired_short_window_does_not_prove_the_layout(self):
        self._sample_with_expired_short()
        with self.assertRaisesRegex(AmbiguousCodexGauges, "short window"):
            self.book().codex_gauges()
        reason = self.book().pace_exceeded("codex")
        self.assertIn("incomplete Codex account reading", reason)
        self.assertIn("short window", reason)

    def test_a_live_short_window_in_the_sample_still_counts(self):
        # Contrôle positif : la même rafale de journaux partiels, mais la
        # courte vivante est la plus récente et reste dans l'échantillon.
        for i in range(codex_gauges.MAX_FILES):
            journal(self.codex, "partial-%d" % i,
                    event(self.now, short_absent=True, week=49),
                    mtime=self.now + i + 1)
        journal(self.codex, "live",
                event(self.now, short=100, week=49), mtime=self.now + 100)
        gauges = self.book().codex_gauges()
        self.assertEqual({g.key: g.used for g in gauges},
                         {"codex-300min": 1.0, "codex-10080min": 0.49})
        self.assertIn("codex-300min", self.book().pace_exceeded("codex"))

    def test_a_live_pro_reading_stays_exempt_beside_expired_team_journals(self):
        # Changement de forfait : un ancien journal team échu reste dans
        # l'échantillon tronqué, mais le forfait vivant est pro — qui ne
        # publie légitimement que la semaine. L'échue team ne doit pas
        # réclamer une courte vivante.
        journal(self.codex, "old-team",
                event(self.now - 10 * DAY, plan="team", short=100,
                      short_reset=self.now - 10 * DAY + 5 * HOUR,
                      week=90, week_reset=self.now - 9 * DAY),
                mtime=self.now + 1)
        journal(self.codex, "old-pro",
                event(self.now - 2 * DAY, plan="pro", short_absent=True,
                      week=90, week_reset=self.now - DAY),
                mtime=self.now)
        for i in range(codex_gauges.MAX_FILES - 1):
            journal(self.codex, "pro-%d" % i,
                    event(self.now, plan="pro", short_absent=True, week=49),
                    mtime=self.now + i + 2)
        gauges = self.book().codex_gauges()
        live = [g.key for g in gauges if not g.reset_passed(self.now)]
        self.assertEqual(live, ["codex-10080min"])
        self.assertTrue(accounts.placeable(evaluation(gauges), self.now))


if __name__ == "__main__":
    unittest.main()
