# SPDX-License-Identifier: AGPL-3.0-only
"""Un avertissement SLO ne reste pas chez son auteur en sommeil (#6396)."""
import unittest

from ameesh import alert_briefs
from .test_3036_alert_liveness import _row, _slo


class SleepingAuthorTest(unittest.TestCase):
    def route(self, *, paused=True, covered=(), pilot_paused=False):
        author = dict(_row("dev1"), operator_paused=paused,
                      operator_pause_by="ameesh:file-decoupage",
                      operator_pause_reason="automatic sleep")
        pilot = dict(_row("orch"), operator_paused=pilot_paused)
        alert = _slo(
            level=1, targets=("assignee",), priority=1,
            lot_state="validated", step="verdict_merge",
            waiting={"what": "merge", "candidate": {
                "code": "freeze-not-head", "owner": "author"}})
        return alert_briefs.briefs(
            alert, [author, pilot, _row("auditeur")],
            auditor_names=["auditeur"], orchestrator_names=("orch",),
            covered=covered)

    def test_p1_freeze_not_head_goes_to_pilot_despite_live_author_lease(self):
        briefs = self.route()
        self.assertEqual(set(briefs), {"orch"})
        self.assertIn("dev1 (operator paused)", briefs["orch"])
        self.assertIn("Escalated from level 1 to level 2", briefs["orch"])
        self.assertIn("freeze-not-head", briefs["orch"])

    def test_previous_delivery_to_sleeping_author_does_not_cover_the_step(self):
        self.assertEqual(set(self.route(covered={"dev1"})), {"orch"})

    def test_awake_author_still_receives_level_one(self):
        self.assertEqual(set(self.route(paused=False)), {"dev1"})

    def test_sleeping_pilot_does_not_trap_the_escalation(self):
        self.assertEqual(set(self.route(pilot_paused=True)), {"auditeur"})

    def test_other_briefs_keep_their_existing_liveness_rule(self):
        author = dict(_row("dev1"), operator_paused=True)
        live, dead = alert_briefs._split_live(
            {"dev1": "ordinary mail"}, {"dev1": author}, set())
        self.assertEqual(live, {"dev1": "ordinary mail"})
        self.assertEqual(dead, {})
