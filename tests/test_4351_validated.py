# SPDX-License-Identifier: AGPL-3.0-only
"""Reader-first compatibility: understand validated without producing it."""
import datetime
import types
from unittest import mock

from ameesh import planned_waits, progress, storage, work, work_dependencies, work_lifecycle
from .support import PgTestCase

HEAD = "a" * 40
NEXT = "b" * 40


class ValidatedTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.lot = work.add(self.db, title="validation", app="demo")["id"]
        work.move(self.db, self.lot, "build")
        work.move(self.db, self.lot, "qa")
        self.ops = storage.of(self.db).work
        self.mark("frozen")

    def mark(self, kind="verdict", sha=HEAD, verdict="ok"):
        return self.ops.add_milestone(
            self.lot, kind, sha=sha, actor="reader", verdict=verdict if kind == "verdict" else None,
            note="", proof=None)

    def current_state(self):
        return work.get(self.db, self.lot)["state"]

    def existing_validated(self):
        # Model rows produced by the later writer release. This reader tranche
        # must support those rows after that release rolls back to its baseline.
        self.db.execute("UPDATE work_items SET state = 'validated' WHERE id = %s", (self.lot,))

    def test_verdict_keeps_legacy_qa_even_with_a_quorum(self):
        with mock.patch("ameesh.merge_queue.review_gate_for_item", return_value={"ok": True}) as gate:
            self.mark()
        gate.assert_not_called()
        self.assertEqual(self.current_state(), "qa")

    def test_manual_writer_is_unavailable_in_the_reader_release(self):
        self.mark()
        with self.assertRaisesRegex(work.WorkError, "transition refused"):
            work.move(self.db, self.lot, "validated")
        self.assertEqual(self.current_state(), "qa")

    def test_refreeze_resets_existing_validation_in_the_same_transaction(self):
        self.existing_validated()
        self.mark("frozen", NEXT)
        self.assertEqual(self.current_state(), "qa")

    def test_verdict_returns_existing_validation_to_qa(self):
        self.existing_validated()
        self.mark(verdict="blocked")
        self.assertEqual(self.current_state(), "qa")

    def test_failed_freeze_hook_rolls_back_state_and_milestone(self):
        self.existing_validated()
        before = self.ops.milestones(self.lot, None)
        def fail(*args):
            raise RuntimeError("designation failed")
        with self.assertRaises(RuntimeError):
            self.ops.add_milestone(self.lot, "frozen", sha=NEXT, actor="author",
                                   verdict=None, note="", on_frozen=fail)
        self.assertEqual(self.current_state(), "validated")
        self.assertEqual(self.ops.milestones(self.lot, None), before)

    def test_existing_validation_can_return_to_build(self):
        self.existing_validated()
        work.move(self.db, self.lot, "build")
        self.assertEqual(self.current_state(), "build")
        self.assertEqual(work.get(self.db, self.lot)["loops"], 1)

    def test_validated_lot_can_declare_a_landing(self):
        self.mark()
        self.existing_validated()
        vehicle = work.add(self.db, title="vehicle", app="demo")["id"]
        self.ops.add_milestone(vehicle, "frozen", sha=NEXT, actor="pilot", verdict=None, note="")
        until = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        work_dependencies.land(self.db, self.lot, vehicle, until_on=until, actor="pilot")
        self.assertEqual(self.current_state(), "validated")

    def test_validated_packet_member_keeps_recorded_membership(self):
        carrier = dict(id=90, state="validated", branch="integration/paquet-test", branch_head=NEXT)
        unit = dict(id=1, state="validated", branch_head=HEAD)
        store = types.SimpleNamespace(work=mock.Mock(), progress=mock.Mock())
        store.work.packet_members.return_value = [dict(package_id=90, work_item_id=1,
                                                       head_sha=NEXT, source_sha=HEAD)]
        store.progress.lot_milestones.return_value = []
        self.assertEqual(planned_waits.recorded_packet_attachments(store, [carrier], [unit]),
                         {90: {1}})

    def test_stored_validation_is_approved_and_not_delivered(self):
        self.assertIn("validated", work_lifecycle.TO_VALIDATE)
        self.assertNotIn("validated", work_lifecycle.COMPLETED)
        self.assertEqual(work_lifecycle.canonical("validated"), "validated")
        self.assertEqual(progress.lot_state("validated", {}), "approved")
