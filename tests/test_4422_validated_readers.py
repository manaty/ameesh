# SPDX-License-Identifier: AGPL-3.0-only
"""Secondary views retain validated lots and explain reviewer changes."""
import unittest
from unittest import mock

from ameesh import debit, lot_measures, resolutions, work
from . import test_3070_device_gates as gates


class ValidatedReadersTest(unittest.TestCase):
    def test_reopened_then_validated_is_still_open(self):
        events = [dict(state="build", at_ts=2), dict(state="validated", at_ts=3)]
        self.assertTrue(debit._reopened(events, 1))
        self.assertFalse(debit._reopened(events + [dict(state="merged", at_ts=4)], 1))

    def test_validated_wait_counts_as_review_not_agent_work(self):
        result = lot_measures.split(0, 120, events=[dict(state="validated", created_ts=0)],
                                    milestones=[], ci_runs=[])
        self.assertEqual(result["review_minutes"], 2)
        self.assertEqual(result["agent_minutes"], 0)

    def test_validated_lands_before_qa_for_conflict_resolution(self):
        result = resolutions.choose_resolver(dict(work_item=1, state="validated"),
                                              dict(work_item=2, state="qa"), [])
        self.assertEqual(result["lot"], 2)

    def test_reviewer_change_explains_return_to_qa(self):
        with mock.patch.object(work, "corrector", return_value="pilot"), \
             mock.patch.object(work, "get", return_value=dict(state="validated")):
            with self.assertRaisesRegex(work.WorkError, "move it to qa before changing reviewers"):
                work._review_item(None, None, 1, "pilot")

    def test_concurrent_validation_explains_return_to_qa(self):
        store = mock.Mock()
        store.work.designate_reviewer.return_value = None
        with mock.patch.object(work.storage, "of", return_value=store), \
             mock.patch.object(work, "get", return_value=dict(state="validated")):
            with self.assertRaisesRegex(work.WorkError, "became validated.*move it to qa"):
                work._write_reviewer(None, 1, "reader", "pilot", "designation")

    def test_device_gate_returns_to_validated(self):
        case = gates.AssessTest()
        events = [gates.ev(1, "validated", "quorum"),
                  gates.ev(2, "blocked", "séance: nx-carina — cas iOS")]
        view = case.assess(events, gates.host("nx-carina", gates.FUTURE, "absente"))
        self.assertTrue(view["liftable"])
        self.assertEqual(view["back_to"], "validated")


from .support import PgTestCase


class ValidatedResumeTest(PgTestCase):
    def test_blocked_and_waiting_human_resume_validation(self):
        for held in ("blocked", "waiting_human"):
            with self.subTest(held=held):
                ident = work.add(self.db, title="resume validation")["id"]
                self.db.execute("UPDATE work_items SET state = %s WHERE id = %s", (held, ident))
                with mock.patch("ameesh.merge_queue.review_gate_for_item", return_value={"ok": True}) as gate:
                    work.move(self.db, ident, "validated")
                gate.assert_called_once()
                self.assertEqual(work.get(self.db, ident)["state"], "validated")

    def test_resume_without_current_reviews_is_refused(self):
        for held in ("blocked", "waiting_human"):
            with self.subTest(held=held):
                ident = work.add(self.db, title="unreviewed resume")["id"]
                self.db.execute("UPDATE work_items SET state = %s WHERE id = %s", (held, ident))
                with self.assertRaisesRegex(work.WorkError, "move it to qa"):
                    work.move(self.db, ident, "validated")
                self.assertEqual(work.get(self.db, ident)["state"], held)
