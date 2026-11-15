# SPDX-License-Identifier: AGPL-3.0-only
"""#7511: debit and SLO share separate wake deadlines; missing dates are visible."""
import datetime
import unittest

from ameesh import planned_waits, work
from .test_3437_debit_dependance import H, T
from .test_3498_hold_externe import event, item, run
from .test_789_slo_alerts import _Base

NOTE = "hold: fenêtre : relire les sondes"


def utc(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC")


class WindowDeadlineTest(unittest.TestCase):
    def test_debit_and_expired_window_read_same_separate_wake(self):
        hold = {"pilot": True, "by": "pilote", "since": T - 4 * H, "note": NOTE}
        for due in (T - H, T + H):
            with self.subTest(due=due):
                events = [event(7511, 1, T - 4 * H, NOTE, actor="pilote"),
                          event(7511, 2, T - 3 * H,
                                "wake: relire (due %s)" % utc(due), actor="pilote")]
                report = run(items=[item(7511)], events=events,
                             holds={7511: {"pilote": hold}})
                slo_events = [{**e, "created_ts": e["at_ts"]} for e in events]
                for e in slo_events:
                    del e["at_ts"]
                expired = planned_waits.expired_window(
                    {"pilote": hold}, T, events=slo_events)
                if due < T:
                    self.assertEqual(expired, due)
                    self.assertEqual(report["window_waits"], [])
                else:
                    self.assertIsNone(expired)
                    self.assertEqual(report["window_waits"][0]["due_ts"], due)

    def test_missing_date_is_unreadable_and_stays_an_agent_wait(self):
        report = run(items=[item(7511)], events=[
            event(7511, 1, T - 4 * H, NOTE, actor="pilote")])
        self.assertEqual(report["window_waits"], [])
        self.assertIn("hold", [r["kind"] for r in report["slow_waits"]])
        self.assertTrue(any("observation window due date unreadable: missing deadline"
                            in line for line in report["unreadable"]))

    def test_ordinary_hold_is_not_an_unreadable_window(self):
        report = run(items=[item(7511)], events=[
            event(7511, 1, T - 4 * H, "hold: attente opérateur", actor="pilote")])
        self.assertFalse(any("observation window" in line for line in report["unreadable"]))


class WindowSloTest(_Base):
    def prepare(self, *, actor="human:pilote", cancelled=False):
        self.age(10 * H)
        due = self.now - 30 * 60
        wake = "wake: relire (due %s)" % utc(due)
        notes = [(NOTE, "human:pilote", 9 * H), (wake, actor, 8 * H)]
        if cancelled:
            notes.append(("wake: cancelled", actor, H))
        for note, author, age in notes:
            work.note(self.db, self.lot["id"], note, actor=author)
            self.db.execute(
                "UPDATE work_item_events SET created_at = to_timestamp(%s) "
                "WHERE work_item_id = %s AND note = %s",
                (self.now - age, self.lot["id"], note))

    def intake(self):
        return [a["level"] for a in self.slo() if a["step"] == "intake_started"]

    def test_separate_wake_restarts_slo_from_deadline(self):
        self.prepare()
        self.assertEqual(self.intake(), [])
        self.now += H
        self.assertEqual(self.intake(), [1])

    def test_other_author_wake_does_not_lift_window(self):
        self.prepare(actor="human:autre")
        self.now += H
        self.assertEqual(self.intake(), [])

    def test_cancelled_wake_does_not_lift_window(self):
        self.prepare(cancelled=True)
        self.now += H
        self.assertEqual(self.intake(), [])
