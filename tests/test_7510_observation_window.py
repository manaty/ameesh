# SPDX-License-Identifier: AGPL-3.0-only
"""#7510: programmed wakes date pilot windows, inline or in a later event."""
import unittest

from ameesh import debit, planned_waits
from .test_3437_debit_dependance import H, T
from .test_3498_hold_externe import event, item, run


DUE = "2026-10-24 05:20 UTC"  # T + 2 h
NOTE = "hold: fenêtre : vérifier la composition après la bascule"


class ObservationWindowTest(unittest.TestCase):
    def report(self, *, note=NOTE, actor="pilote", wake_actor="pilote",
               wake_at=None, wake_due=DUE, extra=(), pilot=True):
        since = T - 4 * H
        notes = [event(7500, 1, since, note, actor=actor),
                 event(7500, 2, T - 3 * H if wake_at is None else wake_at,
                       "wake: vérifier la composition (due %s)" % wake_due,
                       actor=wake_actor), *extra]
        holds = {7500: {actor: {"what": "hold", "by": actor,
                               "since": since, "note": note, "pilot": pilot}}}
        return run(items=[item(7500)], events=notes, holds=holds)

    def test_programmed_wake_wins_over_incidental_due_parenthesis(self):
        for incidental in ("18/11", "2026-10-24 00:20 UTC"):
            note = (NOTE + " ; décision owner (échéance %s). " % incidental
                    + "[réveil programmé: %s]" % DUE)
            with self.subTest(incidental=incidental):
                window, due = planned_waits.observation_due(note)
                self.assertEqual(window["due"], DUE)
                self.assertEqual(due, T + 2 * H)
                self.assertEqual(debit.parse_wake_due(note), T + 2 * H)
                report = run(items=[item(6675)], events=[
                    event(6675, 1, T - 4 * H, note, actor="pilote")])
                self.assertEqual(report["slow_waits"], [])
                self.assertEqual(report["window_waits"][0]["due_ts"], T + 2 * H)
                self.assertFalse(any("unreadable" in s for s in report["unreadable"]))

    def test_later_wake_from_same_pilot_dates_the_window(self):
        report = self.report()
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(report["window_waits"][0]["due_ts"], T + 2 * H)
        self.assertEqual(report["window_waits"][0]["declared"],
                         "vérifier la composition après la bascule")

    def test_same_timestamp_separate_wake(self):
        report = self.report(wake_at=T - 4 * H)
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(len(report["window_waits"]), 1)

    def test_rearmed_wake_replaces_the_inline_deadline(self):
        report = self.report(note=NOTE + " (due 2026-10-24 00:20 UTC)")
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(report["window_waits"][0]["due_ts"], T + 2 * H)

    def test_unrelated_or_old_wake_does_not_date_window(self):
        for values in ({"wake_actor": "dev"}, {"wake_at": T - 5 * H},
                       {"pilot": False}, {"note": "hold: attendre la bascule"},
                       {"wake_due": "demain"}):
            with self.subTest(values=values):
                report = self.report(**values)
                self.assertEqual(report["window_waits"], [])
                self.assertIn("hold", [r["kind"] for r in report["slow_waits"]])

    def test_expired_wake_returns_hold_to_agent_waits(self):
        report = self.report(wake_due="2026-10-24 00:20 UTC")
        self.assertEqual(report["window_waits"], [])
        self.assertIn("hold", [r["kind"] for r in report["slow_waits"]])
        self.assertIn("wake_overdue", [r["kind"] for r in report["slow_waits"]])

    def test_cancelled_or_delivered_wake_does_not_date_window(self):
        for text in ("wake: cancelled", "wake: scheduled wake delivered"):
            with self.subTest(text=text):
                report = self.report(extra=[event(7500, 3, T - H, text)])
                self.assertEqual(report["window_waits"], [])
                self.assertIn("hold", [r["kind"] for r in report["slow_waits"]])
