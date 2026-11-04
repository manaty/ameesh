# SPDX-License-Identifier: AGPL-3.0-only
"""Stored validation is derived from the exact-head review gate (#4423, #5016)."""
from unittest import mock

from ameesh import planned_waits, progress, storage, work, work_lifecycle
from .support import PgTestCase

HEAD = "a" * 40
NEXT = "b" * 40


class ValidatedWritersTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.lot = work.add(self.db, title="validation", app="demo")["id"]
        work.move(self.db, self.lot, "build")
        work.move(self.db, self.lot, "qa")
        self.ops = storage.of(self.db).work
        self.gate = mock.patch("ameesh.merge_queue.review_gate_for_item")
        self.read = self.gate.start()
        self.addCleanup(self.gate.stop)
        self.read.return_value = {"ok": False, "accepted": []}
        self.mark("frozen")

    def mark(self, kind="verdict", sha=HEAD, verdict="ok"):
        return self.ops.add_milestone(
            self.lot, kind, sha=sha, actor="reader", verdict=verdict if kind == "verdict" else None,
            note="", proof=None)

    def current_state(self):
        return work.get(self.db, self.lot)["state"]

    def test_quorum_promotes_and_blocker_revokes_validation(self):
        self.mark("frozen")
        self.mark()
        self.assertEqual(self.current_state(), "qa")
        self.read.return_value = {"ok": True, "accepted": [{"actor": "reader"}]}
        self.mark()
        self.assertEqual(self.current_state(), "validated")
        self.assertEqual(self.read.call_args.args[2]["id"], self.lot)
        self.assertEqual(self.read.call_args.args[3][0]["kind"], "verdict")
        self.read.return_value = {"ok": False, "blocked": [{"actor": "reader"}]}
        self.mark(verdict="blocked")
        self.assertEqual(self.current_state(), "qa")

    def test_the_automatic_quorum_does_not_lift_a_pilot_hold(self):
        """#5758 : le quorum `qa → validated` est une écriture de l'outil.

        Il ne lève pas le hold de pilote posé sur le lot : le composeur doit
        continuer à le refuser (`pilot-hold`). La transition est signée par
        l'outil ; si un writer cesse de le faire, ce témoin le dit."""
        work.note(self.db, self.lot, "hold: pose VM du bureau avant paquet",
                  actor="human:pilote")
        self.read.return_value = {"ok": True}
        self.mark()
        self.assertEqual(self.current_state(), "validated")
        events = storage.of(self.db).progress.lot_hold_events(
            [self.lot], planned_waits.HOLD_NOTE_PATTERNS)
        transition = [event for event in events
                      if event["state"] == "validated"][-1]
        self.assertEqual(transition["actor"], "ameesh")
        self.assertTrue(planned_waits.is_automatic_transition(transition))
        self.assertIn(self.lot, planned_waits.pilot_holds(self.db, events))

    def test_refreeze_resets_validation_in_the_same_transaction(self):
        self.read.return_value = {"ok": True}
        self.mark()
        self.assertEqual(self.current_state(), "validated")
        self.mark("frozen", NEXT)
        self.assertEqual(self.current_state(), "qa")

    def test_failed_freeze_hook_rolls_back_state_and_milestone(self):
        self.read.return_value = {"ok": True}
        self.mark()
        before = self.ops.milestones(self.lot, None)
        def fail(*args):
            raise RuntimeError("designation failed")
        with self.assertRaises(RuntimeError):
            self.ops.add_milestone(self.lot, "frozen", sha=NEXT, actor="author",
                                   verdict=None, note="", on_frozen=fail)
        self.assertEqual(self.current_state(), "validated")
        self.assertEqual(self.ops.milestones(self.lot, None), before)

    def test_manual_validation_requires_quorum_and_unreadable_gate_refuses(self):
        for gate in ({"ok": False}, None):
            self.read.return_value = gate
            with self.assertRaisesRegex(work.WorkError, "quorum"):
                work.move(self.db, self.lot, "validated")
            self.assertEqual(self.current_state(), "qa")
        self.read.return_value = {"ok": True}
        work.move(self.db, self.lot, "validated")
        self.assertEqual(self.current_state(), "validated")
        work.move(self.db, self.lot, "build")
        self.assertEqual(work.get(self.db, self.lot)["loops"], 1)

    def test_concurrent_freeze_invalidates_the_computed_quorum(self):
        def freeze_during_gate(*args, **kwargs):
            self.mark("frozen", NEXT)
            return {"ok": True}
        self.read.side_effect = freeze_during_gate
        self.mark()
        self.assertEqual(self.current_state(), "qa")

    def test_stored_validation_is_approved_and_not_delivered(self):
        self.assertIn("validated", work_lifecycle.TO_VALIDATE)
        self.assertNotIn("validated", work_lifecycle.COMPLETED)
        self.assertEqual(work_lifecycle.canonical("validated"), "validated")
        self.assertEqual(progress.lot_state("validated", {}), "approved")

    def test_manual_validation_rejects_a_concurrent_freeze(self):
        def freeze_during_gate(*args, **kwargs):
            self.mark("frozen", NEXT)
            return {"ok": True}
        self.read.side_effect = freeze_during_gate
        with self.assertRaisesRegex(work.WorkError, "quorum"):
            work.move(self.db, self.lot, "validated")
        self.assertEqual(self.current_state(), "qa")

    def test_concurrent_blocker_invalidates_the_computed_quorum(self):
        def blocker_during_gate(*args, **kwargs):
            self.read.side_effect = None
            self.read.return_value = {"ok": False}
            self.mark(verdict="blocked")
            return {"ok": True}
        self.read.side_effect = blocker_during_gate
        self.mark()
        self.assertEqual(self.current_state(), "qa")

    def test_same_head_refreeze_keeps_the_acquired_quorum(self):
        # #5481 : un regel du même SHA ne périme pas les verdicts rendus sur
        # ce contenu immuable. Le gel repasse par `qa`, puis la porte se relit
        # sur la même tête et le quorum la rouvre — aucune nouvelle relecture.
        self.read.return_value = {"ok": True}
        self.mark()
        self.assertEqual(self.current_state(), "validated")
        self.mark("frozen")
        self.assertEqual(self.current_state(), "validated")

    def test_concurrent_note_does_not_cancel_the_quorum(self):
        # #5016 : une note concurrente ne change que `updated_at` ; la garde
        # comparait la ligne entière et laissait le lot en qa à quorum acquis.
        def note_during_gate(*args, **kwargs):
            work.note(self.db, self.lot, "note concurrente", actor="writer")
            return {"ok": True}
        self.read.side_effect = note_during_gate
        self.mark()
        self.assertEqual(self.current_state(), "validated")

    def test_manual_validation_survives_a_concurrent_note(self):
        def note_during_gate(*args, **kwargs):
            work.note(self.db, self.lot, "note concurrente", actor="writer")
            return {"ok": True}
        self.read.side_effect = note_during_gate
        work.move(self.db, self.lot, "validated")
        self.assertEqual(self.current_state(), "validated")

    def test_concurrent_gate_input_still_invalidates_the_quorum(self):
        # La garde ne doit pas s'affaiblir : un champ lu par la porte
        # (l'assigné) change encore l'observation et annule la promotion.
        def reassign_during_gate(*args, **kwargs):
            self.db.execute(
                "UPDATE work_items SET assignee = 'other', updated_at = now()"
                " WHERE id = %s", (self.lot,))
            return {"ok": True}
        self.read.side_effect = reassign_during_gate
        self.mark()
        self.assertEqual(self.current_state(), "qa")

    def test_concurrent_reviewer_designation_invalidates_the_quorum(self):
        # #5016 (revue auditeur) : une désignation de relecteur retire la
        # dispense `-none` en qa, sans jalon ; la porte calculée avec la
        # dispense ne doit pas promouvoir le lot.
        self.db.execute(
            "UPDATE work_items SET reviewer = '-none' WHERE id = %s", (self.lot,))
        def designate_during_gate(*args, **kwargs):
            self.db.execute(
                "UPDATE work_items SET reviewer = 'reader' WHERE id = %s", (self.lot,))
            return {"ok": True}
        self.read.side_effect = designate_during_gate
        self.mark()
        self.assertEqual(self.current_state(), "qa")
