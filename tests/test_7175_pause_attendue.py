# SPDX-License-Identifier: AGPL-3.0-only
"""#7175 : une pause budget qui attend une remise à zéro connue, sans compte
à libérer, est dite une fois et n'est pas relancée avant l'échéance.

mty-amh-impl-11 (agents-3, 12-13/11) : le seul compte codex de l'hôte en
overage, remise à zéro connue au 13/11 08:30Z ; `budget_pause_long` demandait
toutes les heures de « libérer un compte, corriger le profil », geste
impossible. L'alerte dit désormais « pause expected until <reset>, nothing to
do » et garde une clé stable jusqu'à l'échéance ; un autre motif garde le
texte de #1466.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_briefs, budget, exploitation, mail, notify, registry, storage

from .support import PgTestCase

WEEK = 7 * 86400.0
OVERAGE = ("budget : all codex accounts at threshold — primaire: codex plan: in "
           "overage (billed beyond the plan)")


class AlertTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("ouvrier", "codex", cwd=self.tmp)
        self.db.execute("DELETE FROM quota_gauge_readings")

    def _pause(self, texte, minutes=120):
        registry.set_status(self.db, "ouvrier", "blocked", status_text=texte,
                            error="budget")
        self.db.execute(
            "UPDATE agent_registry SET status_since = now() - make_interval(mins => %s)"
            " WHERE name = 'ouvrier'", (int(minutes),))

    def _gauge(self, now, resets_in):
        storage.of(self.db).operations.record_gauges([{
            "harness": "codex", "key": "codex-10080min", "used": 1.0,
            "resets_at": now + resets_in, "window_s": WEEK,
            "produced_at": now - 300, "source": "test", "account": "primaire",
        }])

    def _alertes(self, now):
        return [a for a in exploitation.alerts(self.cfg, self.db, now=now)
                if a["type"] == "budget_pause_long"]

    def test_overage_with_known_reset_says_nothing_to_do(self):
        now = time.time()
        self._gauge(now, 3 * 3600)
        self._pause(OVERAGE, minutes=37 * 60)
        (alerte,) = self._alertes(now)
        self.assertTrue(alerte["waits_reset"])
        self.assertAlmostEqual(alerte["resume_ts"], now + 3 * 3600, delta=2)
        self.assertIn("pause expected until", alerte["detail"])
        self.assertIn("nothing to do", alerte["detail"])
        self.assertIn("Expected action: none", alert_briefs._budget_pause_long(alerte))

    def test_critical_overage_still_requests_action(self):
        now = 1800000000.0
        self._gauge(now, 3 * 3600)
        self._pause(budget.PAUSE_PREFIX + budget.CRITICAL_MARK
                    + OVERAGE[len(budget.PAUSE_PREFIX):])
        self.db.execute("UPDATE agent_registry SET status_since = to_timestamp(%s)"
                        " WHERE name = 'ouvrier'", (now - 7200,))
        (alerte,) = self._alertes(now)
        self.assertNotIn("waits_reset", alerte)
        self.assertNotIn("nothing to do", alerte["detail"])
        self.assertIn("check the caps", alerte["detail"])

    def test_key_survives_status_flicker(self):
        now = time.time()
        self._gauge(now, 3 * 3600)
        self._pause(OVERAGE, minutes=120)
        (first,) = self._alertes(now)
        self._pause(OVERAGE, minutes=90)
        (second,) = self._alertes(now)
        self.assertNotEqual(first["since"], second["since"])
        self.assertEqual(notify.brief_key_of(first), notify.brief_key_of(second))

    def test_reset_passed_keeps_the_1466_alert(self):
        now = time.time()
        self._gauge(now, -3600)
        self._pause(OVERAGE)
        (alerte,) = self._alertes(now)
        self.assertNotIn("waits_reset", alerte)
        self.assertIn("check the caps", alerte["detail"])

    def test_other_reason_keeps_the_1466_alert(self):
        now = time.time()
        self._gauge(now, 3 * 3600)
        self._pause("budget : no codex account to place: secondaire held for overage "
                    "seen on this host — primaire: codex plan: in overage")
        (alerte,) = self._alertes(now)
        self.assertNotIn("waits_reset", alerte)


class ResetFallbackTest(unittest.TestCase):
    """Échéances d'overage sans base : plusieurs fenêtres et plusieurs comptes."""
    NOW = 1800000000.0

    def wait(self, gauges, reason=OVERAGE, *, harness="codex"):
        store = mock.Mock()
        store.operations.gauge_latest.side_effect = lambda **kw: gauges[kw["account"]]
        with mock.patch.object(storage, "of", return_value=store):
            return exploitation._budget_pause_resume(
                object(), {"harness": harness}, reason, self.NOW)

    def deadline(self, gauges, reason=OVERAGE):
        found = self.wait(gauges, reason)
        return None if found is None else found.deadline

    def gauge(self, used, delay):
        return {"used": used, "resets_at_ts": None if delay is None else self.NOW + delay}

    def test_waits_for_last_exhausted_window_ignores_unspent(self):
        self.assertEqual(self.deadline({"primaire": [
            self.gauge(1.0, 3600), self.gauge(1.1, 10800), self.gauge(0.5, 60),
        ]}), self.NOW + 10800)

    def test_undated_exhausted_window_has_no_deadline(self):
        self.assertIsNone(self.deadline({"primaire": [
            self.gauge(1.0, 3600), self.gauge(1.0, None),
        ]}))

    def test_unspent_undated_window_does_not_hold_account(self):
        self.assertEqual(self.deadline({"primaire": [
            self.gauge(1.0, 3600), self.gauge(0.2, None),
        ]}), self.NOW + 3600)

    def test_first_account_released_suffices(self):
        reason = OVERAGE + "; secondaire: codex plan: in overage (billed beyond the plan)"
        found = self.wait({
            "primaire": [self.gauge(1.0, 3600), self.gauge(1.0, 10800)],
            "secondaire": [self.gauge(1.0, 7200)],
        }, reason)
        self.assertEqual(found.deadline, self.NOW + 7200)
        # #7572 : le compte d'où vient l'échéance est nommé, et une remise à
        # zéro est une heure fixe.
        self.assertEqual(found.account, "secondaire")
        self.assertEqual(found.kind, "reset")

    def test_expired_exhausted_window_does_not_hold_account(self):
        self.assertEqual(self.deadline({"primaire": [
            self.gauge(1.0, -3600), self.gauge(1.0, 7200),
        ]}), self.NOW + 7200)

    def test_no_exhausted_window_proves_no_deadline(self):
        self.assertIsNone(self.deadline({"primaire": [self.gauge(0.2, 3600)]}))

    def test_critical_pause_keeps_action_expected(self):
        self.assertIsNone(self.deadline({"primaire": [self.gauge(1.0, 3600)]},
                                       budget.CRITICAL_MARK + OVERAGE))

    def test_a_pause_label_deadline_is_rendered_even_without_all_at_threshold(self):
        # #7572 : le cas réel du 16/11 sur agents-3 — le relevé d'un compte
        # est trop vieux, la raison n'est donc pas « all accounts at
        # threshold », mais elle annonce bien une échéance.
        reason = ("pause (account secondaire, seven_day 59 %% pace) until 16/11 "
                  "17:07@%d; no claude account to place: reading older than every "
                  "open window (tertiaire until 18/11 06:00)" % int(self.NOW + 3600))
        found = self.wait({}, reason, harness="claude")
        self.assertEqual(found.deadline, self.NOW + 3600)
        self.assertEqual(found.account, "secondaire")
        self.assertEqual(found.gauge, "seven_day")
        self.assertEqual(found.kind, "pace")

    def test_a_label_without_nature_has_no_invented_nature(self):
        reason = ("pause (account primaire, five_hour 96 %%) until 16/11 "
                  "17:07@%d; all codex accounts at threshold — primaire: ..."
                  % int(self.NOW + 3600))
        found = self.wait({}, reason, harness="codex")
        self.assertIsNone(found.kind)
        self.assertEqual(found.account, "primaire")


class BriefOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(canon=(), state_dir=self.tmp.name)
        self.store = mock.Mock()
        self.listing = [dict(name=n, team="ameesh", mode="execute")
                        for n in ("ouvrier", "pilot")]
        self.store.operations.listing.side_effect = lambda: self.listing
        for target, attr, value in ((storage, "of", self.store), (mail, "send", 1),
                                    (notify.fil, "record", "thread-id"),
                                    (alert_briefs, "briefs", {"pilot": "pause"})):
            patch = mock.patch.object(target, attr, return_value=value)
            mocked = patch.start()
            self.addCleanup(patch.stop)
            if attr == "send":
                self.send = mocked
        self.notifier = notify.Notifier(self.cfg, notify.parse_config({"default": []}),
                                        sender=object(), log=lambda text: None)

    def alert(self, since, resume_ts, *, kind="pace"):
        return dict(schema="ameesh-alert/1", type="budget_pause_long", agent="ouvrier",
                    since=since, value=7200, threshold=3600, host="ameesh-agents-3",
                    harness="codex", responsible="human:smichea", detail="pause",
                    resume_ts=resume_ts, resume_kind=kind, waits_reset=True,
                    orchestrators=["pilot"])

    def test_said_once_until_the_reset(self):
        run = self.notifier._brief_agents
        run(object(), [self.alert(1000, 40000, kind="reset")], None, 10000)
        run(object(), [], None, 10030)                          # statut qui clignote
        run(object(), [self.alert(10031, 40000, kind="reset")], None, 10060)
        run(object(), [self.alert(10031, 40000, kind="reset")], None, 10000 + 7 * 3600)
        self.assertEqual(self.send.call_count, 1)

    def test_after_the_reset_a_new_episode_gets_a_brief(self):
        run = self.notifier._brief_agents
        run(object(), [self.alert(1000, 10500, kind="reset")], None, 10000)
        run(object(), [], None, 10600)
        # #7572 : l'absence doit dépasser la grâce du clignotement de statut
        # pour clore l'épisode — sinon c'est la même pause, dite une fois.
        run(object(), [], None, 10600 + alert_briefs.BUDGET_PAUSE_GAP_S + 1)
        run(object(), [self.alert(20000, 50000, kind="reset")], None, 30000)
        self.assertEqual(self.send.call_count, 2)

    def test_a_pace_deadline_passed_while_the_pause_lasts_is_the_same_brief(self):
        # #7572 : le cas du 16/11 — l'ancienne échéance de rattrapage passe,
        # la pause continue sur une nouvelle estimation : même pause, un seul
        # brief. Une remise à zéro passée, elle, re-briefe (B1, #7572).
        run = self.notifier._brief_agents
        run(object(), [self.alert(1000, 10500)], None, 10000)
        run(object(), [self.alert(1001, 50000)], None, 10600)
        run(object(), [self.alert(1001, 50000)], None, 10700)
        self.assertEqual(self.send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
