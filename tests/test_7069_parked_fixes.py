# SPDX-License-Identifier: AGPL-3.0-only
"""Parked corrections remain debts without repeatedly waking their debtor."""
import time
from unittest import mock

from ameesh import file_decoupage, planned_waits, reprise, storage, work
from ameesh import review_designation_activation as activation

from .test_3288_fixes_owed import _Base


class ParkedFixTest(_Base):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(planned_waits, "lift_authority",
                                  return_value=lambda actor: actor == "pilot")
        patch.start()
        self.addCleanup(patch.stop)
        self.lot, _ = self.blocked()

    def park(self, actor="pilot"):
        work.move(self.db, self.lot, "blocked")
        work.note(self.db, self.lot, "hold: deferred until prerequisite", actor=actor)

    def assert_actionable(self, expected):
        self.assertEqual(bool(activation.actionable_fixes(self.db, "author")), expected)
        lines = reprise.fixes_owed_lines(self.db, "author")
        self.assertEqual(lines != [reprise.NO_FIX_OWED], expected)
        owed, error = file_decoupage.owed_work(self.db, ["author"], [])
        self.assertIsNone(error)
        self.assertEqual("author" in owed, expected)
        self.assertEqual(len(activation.due_fixes(self.db, "author")), 1,
                         "parking must never discharge the review debt")

    def test_pilot_parking_silences_both_wake_paths_until_lifted(self):
        self.assert_actionable(True)
        self.park()
        self.assert_actionable(False)
        storage.of(self.db).work.note(self.lot, "blocked",
                                       "unhold: prerequisite delivered", "pilot")
        self.assert_actionable(True)

    def test_ordinary_hold_or_blocked_state_alone_does_not_park_a_fix(self):
        work.move(self.db, self.lot, "blocked")
        self.assert_actionable(True)
        work.note(self.db, self.lot, "hold: author waiting", actor="author")
        self.assert_actionable(True)

    def test_resuming_build_lifts_parking(self):
        self.park()
        work.move(self.db, self.lot, "build")
        self.assert_actionable(True)

    def test_due_wake_after_hold_reactivates_but_stale_wake_does_not(self):
        self.park()
        for offset, expected in ((-3600, False), (3600, False), (1, True)):
            now = time.time() + (10 if offset == 1 else 0)
            self.db.execute("UPDATE work_items SET wake_at = to_timestamp(%s) WHERE id = %s",
                            (time.time() + offset, self.lot))
            self.assertEqual(bool(activation.actionable_fixes(
                self.db, "author", now=now)), expected)

    def test_unreadable_hold_never_reports_nothing_due(self):
        self.park()
        with mock.patch.object(planned_waits, "pilot_holds", side_effect=RuntimeError("hold read")):
            self.assertIn("fixes owed unreadable", reprise.fixes_owed_lines(self.db, "author")[0])
            owed, error = file_decoupage.owed_work(self.db, ["author"], [])
        self.assertEqual(owed, {})
        self.assertIn("hold read", str(error))
