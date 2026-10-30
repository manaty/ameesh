# SPDX-License-Identifier: AGPL-3.0-only
"""L102 T1 (#4391): notifications rendered in the active module's vocabulary.

notify.py and alert_briefs.py take the work item's labels from
``vocab.active()``: DEV ("lot") through the dev module, CORE ("tâche") when
no active module declares a vocabulary. Alert types, JSON keys and command
names/flags stay unchanged.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import alert_briefs, notify, vocab
from ameesh.modules import dev

STALE = {"type": "stale_lot", "lot": 4391, "title": "L102 T1",
         "lot_state": "qa", "value": 7200, "waiting_for": "fusion",
         "agent": "ameesh-dev5", "targets": ["assignee"]}
LISTING = [{"name": "ameesh-dev5", "workstream": "ameesh", "status": "working"},
           {"name": "orch", "workstream": "ameesh", "status": "working"}]


class _VocabularyCase(unittest.TestCase):
    def setUp(self):
        vocab.reset()
        self.addCleanup(vocab.reset)

    def core(self):
        return mock.patch.object(vocab, "active", return_value=vocab.CORE)


class DefaultVocabularyTest(_VocabularyCase):
    def test_the_dev_module_is_active(self):
        self.assertEqual(vocab.active(), dev.DEV)

    def test_the_pushed_labels_still_say_lot(self):
        self.assertEqual(notify.type_label("orphan_lot"), "orphan lot")
        self.assertEqual(notify.type_label("stale_lot"), "stale lot")
        self.assertEqual(notify.type_label("worktree_kept"),
                         "worktree kept at end of lot")
        self.assertEqual(notify._subject({"lot": 4391, "title": "T"}),
                         'lot #4391 "T"')

    def test_a_label_without_a_work_noun_is_unchanged(self):
        self.assertEqual(notify.type_label("dead_runner"), "dead runner")


class NotifyCoreRenderingTest(_VocabularyCase):
    def test_type_labels(self):
        with self.core():
            self.assertEqual(notify.type_label("orphan_lot"), "orphan tâche")
            self.assertEqual(notify.type_label("stale_lot"), "stale tâche")
            self.assertEqual(notify.type_label("waiting_without_question"),
                             "tâche waiting for the owner with no question asked")
            self.assertEqual(notify.type_label("dead_runner"), "dead runner")

    def test_subject(self):
        with self.core():
            self.assertEqual(notify._subject({"lot": 4391, "title": "T"}),
                             'tâche #4391 "T"')

    def test_pushed_message_keeps_its_type(self):
        alert = dict(STALE)
        with self.core():
            message = notify.render(alert, "raised", 1000.0)
        self.assertEqual(message.kind, "stale_lot")
        self.assertIn("[stale_lot]", message.body)
        self.assertIn("tâche #4391", message.body)
        self.assertIn("ameesh: stale tâche", message.title)
        self.assertNotIn("lot #", message.body)
        self.assertEqual(alert, STALE)

    def test_resolved_message_follows_the_vocabulary(self):
        with self.core():
            message = notify.render(STALE, "resolved", 1000.0)
        self.assertTrue(message.low)
        self.assertIn("resolved — stale tâche", message.title)

    def test_grouped_orphan_lots(self):
        alerts = [{"type": "orphan_lot", "lot": 1, "agent": "a", "reason": "r"},
                  {"type": "orphan_lot", "lot": 2, "agent": "a", "reason": "r"}]
        with self.core():
            grouped = notify.group_orphan_lots(alerts, lambda alert: ("human:x", "lot"))
            message = notify.render(grouped[0], "raised", 1000.0)
        self.assertEqual(grouped[0]["type"], "orphan_lot")
        self.assertEqual(grouped[0]["value"], 2)
        self.assertEqual(grouped[0]["detail"], "2 orphan tâche(s) (r): #1, #2")
        self.assertIn("[orphan_lot]", message.body)
        self.assertEqual(message.title, "ameesh: orphan tâches (agent a)")


class NotifyDevRenderingTest(_VocabularyCase):
    def test_pushed_message_says_lot(self):
        message = notify.render(STALE, "raised", 1000.0)
        self.assertEqual(message.kind, "stale_lot")
        self.assertIn("[stale_lot]", message.body)
        self.assertIn("lot #4391", message.body)
        self.assertIn("ameesh: stale lot", message.title)

    def test_grouped_orphan_lots_say_lots(self):
        alerts = [{"type": "orphan_lot", "lot": 1, "agent": "a", "reason": "r"},
                  {"type": "orphan_lot", "lot": 2, "agent": "a", "reason": "r"}]
        grouped = notify.group_orphan_lots(alerts, lambda alert: ("human:x", "lot"))
        self.assertEqual(grouped[0]["detail"], "2 orphan lot(s) (r): #1, #2")


class BriefsCoreRenderingTest(_VocabularyCase):
    def test_stale_lot_brief(self):
        with self.core():
            text = alert_briefs._stale_lot(STALE)
        self.assertTrue(text.startswith("Stale tâche: #4391"))
        self.assertIn("this tâche is frozen", text)
        # The commands and their flags never change with the vocabulary.
        self.assertIn("ameesh work merged 4391 --sha", text)
        self.assertIn("--vehicle", text)

    def test_reviewer_briefs(self):
        with self.core():
            missing = alert_briefs._reviewer_missing(STALE)
            due = alert_briefs._verdict_due(STALE)
        self.assertIn("No reviewer designated: tâche #4391", missing)
        self.assertIn("Verdict expected: tâche #4391", due)
        self.assertIn("say so on the tâche", due)
        self.assertIn("ameesh work reviewer 4391", missing)

    def test_waiting_without_question(self):
        with self.core():
            text = alert_briefs._waiting_without_question(STALE)
        self.assertTrue(text.startswith("Tâche #4391"))
        self.assertIn("a tâche waiting for them", text)
        self.assertIn("move the tâche back", text)
        self.assertIn("ameesh decide ask --lot 4391", text)

    def test_idle_actionable_brief(self):
        alert = dict(STALE, type="idle_actionable_lot", agent="ameesh-dev5")
        with self.core():
            text = alert_briefs._idle_actionable_lot(alert)
        self.assertIn("Idle with an actionable tâche", text)
        self.assertIn("left the tâche unchanged", text)
        self.assertIn("--lot 4391", text)

    def test_plan_underused_and_estimate_bias(self):
        with self.core():
            underused = alert_briefs._plan_underused(
                {"detail": "plan x", "idle_agents": ["a"]})
            biased = alert_briefs._estimate_bias(
                {"detail": "x", "agent": "a", "value": 2})
        self.assertIn("tied to a tâche", underused)
        self.assertIn("open tâches without an agent", underused)
        self.assertIn("underestimates tâches", biased)

    def test_slo_breach(self):
        alert = {"lot": 4391, "title": "T", "step": "verdict_merge", "level": 1,
                 "lot_state": "qa", "targets": ["assignee"],
                 "waiting": {"what": "merge"}}
        with self.core():
            text = alert_briefs._slo_breach(alert)
        self.assertIn("SLO breach, level 1: tâche #4391", text)
        self.assertIn("the tâche is frozen", text)
        self.assertIn("include it in the next package", text)

    def test_escalation_and_auditor_texts(self):
        with self.core():
            auditor = alert_briefs._for_auditor(
                {"detail": "x", "startable_lots": [1]})
            pause = alert_briefs._operator_pause(
                {"detail": "x", "agent": "a", "reason": "maintenance"})
            chronic = alert_briefs._rate_limited_chronic(
                {"detail": "x", "agent": "a"})
            gap = alert_briefs._notify_undeliverable({"detail": "x"})
        self.assertIn("Tâches are waiting", auditor)
        self.assertIn("of the tâche's team", auditor)
        self.assertIn("--lot <lot>", auditor)
        self.assertIn("urgent tâches", pause)
        self.assertIn("urgent tâches", chronic)
        self.assertIn("ameesh work reviewer <lot>", chronic)
        self.assertIn("tâche threads", gap)

    def test_briefs_route_the_active_vocabulary(self):
        alert = dict(STALE, orchestrators=["orch"], project="ameesh")
        with self.core():
            out = alert_briefs.briefs(alert, LISTING, orchestrator_names=("orch",))
        self.assertEqual(set(out), {"orch"})
        self.assertTrue(out["orch"].startswith("Stale tâche: #4391"))
        self.assertIn("ameesh work landing 4391 --vehicle", out["orch"])


class BriefsDevRenderingTest(_VocabularyCase):
    def test_the_notifications_still_say_lot(self):
        alert = dict(STALE, orchestrators=["orch"], project="ameesh")
        out = alert_briefs.briefs(alert, LISTING, orchestrator_names=("orch",))
        self.assertEqual(set(out), {"orch"})
        self.assertTrue(out["orch"].startswith("Stale lot: #4391"))
        self.assertIn("this lot is frozen", out["orch"])
        self.assertIn("ameesh work landing 4391 --vehicle", out["orch"])
        self.assertIn("Stale lot: #4391", alert_briefs._stale_lot(STALE))

    def test_the_other_briefs_say_lot(self):
        self.assertIn("No reviewer designated: lot #4391",
                      alert_briefs._reviewer_missing(STALE))
        self.assertIn("Verdict expected: lot #4391",
                      alert_briefs._verdict_due(STALE))
        self.assertTrue(alert_briefs._waiting_without_question(STALE)
                        .startswith("Lot #4391"))


if __name__ == "__main__":
    unittest.main()
