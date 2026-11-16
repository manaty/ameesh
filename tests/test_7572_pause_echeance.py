# SPDX-License-Identifier: AGPL-3.0-only
"""#7572 : une pause budget annonce une échéance datée et nommée, et l'alerte
« dite une fois » ne se répète plus quand cette échéance glisse.

16/11, agents-3 : six agents Claude en pause budget depuis ~55 h. Leur raison
annonçait « pause (account secondaire, seven_day 59 %) until 17:07@… » mais
l'alerte `budget_pause_long` disait seulement « check the caps » — pas de
date UTC, pas de compte, pas de nature. Et sa clé contenait `since` et la
raison (pourcentage, heure) : chaque relevé refaisait un courrier
(11:16Z → 12:57Z → 17:07Z).

L'échéance est désormais rendue avec sa nature — remise à zéro de la fenêtre
(« reset », fixe) ou rattrapage du rythme (« pace », révisable) — et son
compte ; l'identité de l'alerte est la pause continue (agent, hôte), donc un
recul ou un dépassement de l'échéance ne refait pas de courrier.
"""
from __future__ import annotations

import time
import types
import unittest

from ameesh import accounts, alert_briefs, exploitation, notify, registry, storage

from .support import PgTestCase

WEEK = 7 * 86400.0
OVERAGE = ("budget : all claude accounts at threshold — primaire: claude plan: in "
           "overage (billed beyond the plan)")


class LabelNatureTest(unittest.TestCase):
    def evaluation(self, kind="pace", name="secondaire"):
        return types.SimpleNamespace(
            until=1800003600.0, until_kind=kind, held=("seven_day", 0.59),
            profile=types.SimpleNamespace(name=name))

    def test_the_label_says_the_nature_and_keeps_the_epoch(self):
        for kind in ("reset", "pace"):
            with self.subTest(kind=kind):
                label = accounts.pause_label({"secondaire": self.evaluation(kind)})
                self.assertIn(" %s)" % kind, label)
                self.assertIn("until", label)
                self.assertEqual(accounts.pause_deadline(label), 1800003600.0)
                self.assertEqual(accounts.pause_deadline_nature(label), kind)

    def test_a_label_without_nature_is_not_invented(self):
        ev = self.evaluation()
        ev.until_kind = None
        label = accounts.pause_label({"secondaire": ev})
        self.assertIsNone(accounts.pause_deadline_nature(label))
        self.assertIn("seven_day 59 %) until", label)
        self.assertEqual(accounts.pause_deadline(label), 1800003600.0)

    def test_a_nature_change_changes_the_note_nature(self):
        pace = "pause (account secondaire, seven_day 59 % pace) until 16/11 17:07; …"
        reset = "pause (account secondaire, seven_day 59 % reset) until 16/11 17:07; …"
        self.assertNotEqual(accounts.pause_nature(pace), accounts.pause_nature(reset))
        self.assertEqual(accounts.pause_nature(pace).split("|")[4], "pace")
        self.assertEqual(accounts.pause_nature(reset).split("|")[4], "reset")


class AlertIdentityTest(unittest.TestCase):
    def alert(self, *, since=1000, deadline=40000, detail="pause",
              agent="claude1", host="ameesh-agents-3", waits_reset=False,
              resume_kind="pace"):
        out = dict(type="budget_pause_long", agent=agent, host=host, since=since,
                   resume_ts=deadline, resume_kind=resume_kind, detail=detail)
        if waits_reset:
            out["waits_reset"] = True
        return out

    def test_a_sliding_deadline_or_a_new_reason_keeps_the_same_alert(self):
        # #7572 : c'était la cause du courrier par relevé.
        first = self.alert()
        later = self.alert(
            since=2000, deadline=60000,
            detail="pause (account primaire, five_hour 63 %) until 18:30")
        self.assertEqual(exploitation.alert_key(first), exploitation.alert_key(later))
        self.assertEqual(notify.brief_key_of(first), notify.brief_key_of(later))

    def test_the_payload_does_not_separate_the_same_pause(self):
        self.assertEqual(
            exploitation.alert_key(self.alert()),
            exploitation.alert_key(self.alert(waits_reset=True, resume_kind="reset")))

    def test_agent_and_host_still_separate_two_pauses(self):
        first = self.alert()
        self.assertNotEqual(exploitation.alert_key(first),
                            exploitation.alert_key(self.alert(agent="claude2")))
        self.assertNotEqual(exploitation.alert_key(first),
                            exploitation.alert_key(self.alert(host="ameesh-agents-1")))


class ResumeAlertTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("ouvrier", "claude", cwd=self.tmp)
        self.db.execute("DELETE FROM quota_gauge_readings")

    def _pause(self, texte, minutes=26 * 60):
        registry.set_status(self.db, "ouvrier", "blocked", status_text=texte,
                            error="budget")
        self.db.execute(
            "UPDATE agent_registry SET status_since = now() - make_interval(mins => %s)"
            " WHERE name = 'ouvrier'", (int(minutes),))

    def _alert(self, now):
        rows = [a for a in exploitation.alerts(self.cfg, self.db, now=now)
                if a["type"] == "budget_pause_long"]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def _label(self, now, kind="pace"):
        deadline = (int(now + 25 * 3600) // 60) * 60
        ev = types.SimpleNamespace(
            until=deadline, until_kind=kind, held=("seven_day", 0.59),
            profile=types.SimpleNamespace(name="secondaire"))
        return deadline, accounts.pause_label({"secondaire": ev})

    def test_a_pace_deadline_is_announced_with_its_account_and_nature(self):
        now = time.time()
        deadline, label = self._label(now)
        self._pause("budget : " + label + (
            "no claude account to place: reading older than every open window "
            "(tertiaire until 18/11 06:00)"))
        alerte = self._alert(now)
        self.assertEqual(int(alerte["resume_ts"]), deadline)
        self.assertEqual(alerte["resume_account"], "secondaire")
        self.assertEqual(alerte["resume_kind"], "pace")
        self.assertNotIn("waits_reset", alerte)
        detail = alerte["detail"]
        self.assertIn("pause expected until", detail)
        self.assertIn(exploitation._utc_label(deadline), detail)
        self.assertIn("on account secondaire", detail)
        self.assertIn("pace catch-up", detail)
        self.assertIn("may recede", detail)
        self.assertIn("check the caps", detail)
        brief = alert_briefs._budget_pause_long(alerte)
        self.assertIn("sent once for the pause", brief)
        self.assertNotIn("A new alert follows", brief)

    def test_a_reset_deadline_says_fixed_and_nothing_to_do(self):
        now = time.time()
        resets = now + 3 * 3600
        storage.of(self.db).operations.record_gauges([{
            "harness": "claude", "key": "seven_day", "used": 1.0,
            "resets_at": resets, "window_s": WEEK,
            "produced_at": now - 300, "source": "test", "account": "primaire",
        }])
        self._pause(OVERAGE)
        alerte = self._alert(now)
        self.assertAlmostEqual(alerte["resume_ts"], resets, delta=2)
        self.assertEqual(alerte["resume_account"], "primaire")
        self.assertEqual(alerte["resume_kind"], "reset")
        self.assertTrue(alerte["waits_reset"])
        detail = alerte["detail"]
        self.assertIn("window reset, a fixed time", detail)
        self.assertIn("nothing to do", detail)
        self.assertIn("Expected action: none", alert_briefs._budget_pause_long(alerte))
        self.assertIn("live detail with `ameesh alerts`",
                      alert_briefs._budget_pause_long(alerte))


if __name__ == "__main__":
    unittest.main()
