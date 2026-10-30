# SPDX-License-Identifier: AGPL-3.0-only
"""#3556 : les lots sous hold pilote restent hors prise et proposition."""
from unittest.mock import patch

from ameesh import backlog, storage, work
from .test_l165_prise_lots import _Lots


class PilotHoldTest(_Lots):
    def held(self):
        lot = self.lot("spec attendue", priority=1)
        storage.of(self.db).work.note(
            lot["id"], "intake", "hold: spec attendue", "human:proprio")
        return lot

    def test_hold_excludes_dry_run_and_take_then_lift_releases(self):
        self.agent("claude1", team="nexlink")
        lot = self.held()
        self.assertEqual(self.take(dry_run=True), [])
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        storage.of(self.db).work.note(
            lot["id"], "intake", "unhold: spec recue", "human:proprio")
        self.assertEqual([t["item"]["id"] for t in self.take()], [lot["id"]])

    def test_hold_of_another_lot_does_not_exclude_ready_lot(self):
        self.agent("claude1", team="nexlink")
        held = self.held()
        ready = self.lot("pret", priority=2)
        self.assertEqual([t["item"]["id"] for t in self.take()], [ready["id"]])
        self.assertIsNone(work.get(self.db, held["id"])["assignee"])

    def test_wake_deadline_releases_only_when_due(self):
        self.agent("claude1", team="nexlink")
        lot = self.held()
        st = storage.of(self.db)
        st.work.schedule_wake(lot["id"], self.now + 60, text="rendez-vous", actor="test")
        self.assertEqual(self.take(dry_run=True), [])
        self.assertEqual([t["item"]["id"] for t in self.take(now=self.now + 120)],
                         [lot["id"]])

    def test_stale_wake_before_hold_does_not_release(self):
        """Un réveil échu AVANT la pose du hold (lot rendu à la file, réveil
        jamais consommé faute d'assigné) ne lève pas le hold posé ensuite."""
        self.agent("claude1", team="nexlink")
        lot = self.lot("spec attendue", priority=1)
        storage.of(self.db).work.schedule_wake(
            lot["id"], self.now - 3600, text="ancien rendez-vous", actor="test")
        storage.of(self.db).work.note(
            lot["id"], "intake", "hold: spec attendue", "human:proprio")
        self.assertEqual(self.take(dry_run=True), [])
        self.assertEqual(self.take(), [])

    def test_roadmap_uses_same_hold_filter(self):
        lot = self.held()
        with patch("ameesh.sous_utilisation.waiting_project_lots", side_effect=lambda rows, **kw: rows):
            self.assertEqual(backlog.roadmap_waiting(self.db, "nexlink", now=self.now), [])
            storage.of(self.db).work.note(
                lot["id"], "intake", "unhold: spec recue", "human:proprio")
            self.assertEqual([r["id"] for r in backlog.roadmap_waiting(
                self.db, "nexlink", now=self.now)], [lot["id"]])

    def test_non_pilot_hold_does_not_reserve_team_queue(self):
        self.agent("claude1", team="nexlink")
        lot = self.lot("pret")
        storage.of(self.db).work.note(
            lot["id"], "intake", "hold: note auteur", "claude1")
        self.assertEqual([t["item"]["id"] for t in self.take()], [lot["id"]])

    def test_unreadable_hold_authority_prevents_take(self):
        self.agent("claude1", team="nexlink")
        lot = self.held()
        with patch("ameesh.planned_waits._pilot_allowed", side_effect=RuntimeError("canon unreadable")):
            with self.assertRaisesRegex(RuntimeError, "canon unreadable"):
                self.take()
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])


class ImprovementQueueHoldTest(_Lots):
    """#4741 : la file d'amélioration suit la garde des lots d'équipe."""

    def held_element(self, title="amelioration tenue", *, score=15, wake=True):
        item = self.element(title, score)
        st = storage.of(self.db)
        st.work.note(item["id"], "intake", "hold: fenêtre jusqu'au 04/11",
                     "human:proprio")
        if wake:
            st.work.schedule_wake(item["id"], self.now + 3600,
                                  text="réveil", actor="test")
        return item

    def test_held_improvement_with_future_wake_is_not_taken(self):
        self.agent("claude1", team="nexlink")
        item = self.held_element()
        self.assertEqual(self.take(dry_run=True), [])
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, item["id"])["assignee"])

    def test_lift_releases_held_improvement(self):
        self.agent("claude1", team="nexlink")
        item = self.held_element()
        self.assertEqual(self.take(), [])
        storage.of(self.db).work.note(
            item["id"], "intake", "unhold: fenêtre ouverte", "human:proprio")
        self.assertEqual([t["item"]["id"] for t in self.take()], [item["id"]])

    def test_wake_deadline_releases_held_improvement(self):
        self.agent("claude1", team="nexlink")
        item = self.held_element()
        self.assertEqual(self.take(), [])
        self.assertEqual([t["item"]["id"] for t in self.take(now=self.now + 7200)],
                         [item["id"]])

