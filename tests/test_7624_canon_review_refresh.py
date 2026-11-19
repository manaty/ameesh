# SPDX-License-Identifier: AGPL-3.0-only
"""An audited canon move completes in-flight frozen review designations."""
from __future__ import annotations

import dataclasses
import os
import types
import unittest
from unittest import mock

from ameesh import review_canon_refresh as refresh
from ameesh import review_designation_activation as act
from ameesh import work
from ameesh.work_reviewers import designated

from . import test_2447_review_designation_activation as base
from .support import PgTestCase
from .test_2487_designation_carry import _Fixture as _CarryFixture


class CanonReviewRefreshTest(base._ActivationFixture, PgTestCase):
    def setUp(self):
        super().setUp()
        self.canon = base._Canon(
            os.path.join(self.tmp, "one-review"),
            federation=base.FEDERATION.replace("            reviews: 2",
                                               "            reviews: 1"))
        self.cfg = dataclasses.replace(self.cfg, canon=self.canon.root, canon_ref="HEAD")

    def raise_policy(self):
        self.canon.update("federation.yaml", base.FEDERATION)

    def test_qa_lot_receives_only_the_missing_review_before_composition(self):
        frozen, initial = self.freeze()
        first = initial["reviewers"][0]["name"]
        self.assertEqual(initial["required"], 1)
        opinion = work.milestone(self.db, self.lot, "verdict", sha=self.frozen,
                                 actor=first, verdict="ok", repo=self.repo.root,
                                 after_commit=[])
        before = len(self.mailbox())
        self.raise_policy()
        result = refresh.after_sync(self.cfg, self.db)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(result["refreshed"], 1, result)
        names = designated(work.get(self.db, self.lot))
        self.assertEqual(len(names), 2, names)
        self.assertIn(first, names)
        added = [name for name in names if name != first]
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]], added)
        self.assertEqual(act._latest_freeze(self.db, self.lot)["id"], frozen["id"])
        self.assertTrue(any(row["id"] == opinion["id"] and row["verdict"] == "ok"
                            for row in self.db.query(
                                "SELECT id, verdict FROM work_item_milestones"
                                " WHERE work_item_id = %s", (self.lot,))))
        before = len(self.mailbox())
        for _ in range(2):
            again = refresh.after_sync(self.cfg, self.db)
            self.assertEqual(again["errors"], [], again)
            self.assertEqual(again["refreshed"], 0, again)
        self.assertEqual(len(self.mailbox()), before)

    def test_validated_packet_member_is_rechecked(self):
        self.freeze()
        # Existing packet member admitted under the former canon policy.
        self.db.execute("UPDATE work_items SET state = 'validated' WHERE id = %s",
                        (self.lot,))
        self.raise_policy()
        result = refresh.after_sync(self.cfg, self.db)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(len(designated(work.get(self.db, self.lot))), 2)

    def test_unavailable_second_family_alerts_pilot_then_recovers(self):
        _, initial = self.freeze()
        first = initial["reviewers"][0]["name"]
        other = "r2" if first == "r1" else "r1"
        self.db.execute("UPDATE agent_registry SET status = 'stopped' WHERE name = %s",
                        (other,))
        self.raise_policy()
        before = len(self.mailbox())
        refresh.after_sync(self.cfg, self.db)
        refresh.after_sync(self.cfg, self.db)
        self.assertEqual(designated(work.get(self.db, self.lot)), [first])
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]], ["pilot"])
        self.db.execute("UPDATE agent_registry SET status = 'idle' WHERE name = %s",
                        (other,))
        result = refresh.after_sync(self.cfg, self.db)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(len(designated(work.get(self.db, self.lot))), 2)
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]], ["pilot", other])

    def test_unreadable_policy_notices_pilot_and_remains_retryable(self):
        self.freeze()
        before = len(self.mailbox())
        with mock.patch.object(refresh, "_refs", side_effect=act.NoCanon("unreadable")):
            for _ in range(2):
                result = refresh.after_sync(self.cfg, self.db)
                self.assertTrue(result["errors"], result)
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]], ["pilot"])
        self.raise_policy()
        result = refresh.after_sync(self.cfg, self.db)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(len(designated(work.get(self.db, self.lot))), 2)

    def test_same_count_move_is_checked_once_and_never_resends(self):
        self.freeze()
        before = len(self.mailbox())
        self.canon.update("index.md", "# New canon revision\n")
        first = refresh.after_sync(self.cfg, self.db)
        self.assertEqual(first["errors"], [], first)
        self.assertEqual(first["refreshed"], 1, first)
        with mock.patch.object(act, "prepare", side_effect=AssertionError("already checked")):
            again = refresh.after_sync(self.cfg, self.db)
        self.assertEqual(again["errors"], [], again)
        self.assertEqual(again["refreshed"], 0, again)
        self.assertEqual(len(self.mailbox()), before)

    def test_lowered_policy_keeps_all_existing_seats(self):
        self.raise_policy()
        _, initial = self.freeze()
        self.assertEqual(initial["required"], 2)
        names = designated(work.get(self.db, self.lot))
        before = len(self.mailbox())
        self.canon.update("federation.yaml", base.FEDERATION.replace(
            "            reviews: 2", "            reviews: 1"))
        result = refresh.after_sync(self.cfg, self.db)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(designated(work.get(self.db, self.lot)), names)
        self.assertEqual(len(self.mailbox()), before)

    def test_completed_lots_are_not_reopened(self):
        self.freeze()
        self.db.execute("UPDATE work_items SET state = 'closed', close_reason = 'done' WHERE id = %s",
                        (self.lot,))
        self.raise_policy()
        with mock.patch.object(act, "designate", side_effect=AssertionError("closed")):
            result = refresh.after_sync(self.cfg, self.db)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(result["examined"], 0, result)

    def test_bounded_scan_reaches_later_lots_and_wraps(self):
        self.freeze()
        first = self.lot
        item = work.add(self.db, title="Second frozen lot", app="ameesh", assignee="author")
        self.lot = int(item["id"])
        work.move(self.db, self.lot, "build")
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.repo.write("src/ameesh/mail.py", "second lot\n")
        self.frozen = self.repo.commit("second lot")
        self.receipts()
        self.freeze()
        self.raise_policy()
        result = refresh.after_sync(self.cfg, self.db, limit=1)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(len(designated(work.get(self.db, first))), 2)
        self.assertEqual(len(designated(work.get(self.db, self.lot))), 1)
        result = refresh.after_sync(self.cfg, self.db, limit=1)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(len(designated(work.get(self.db, self.lot))), 2)
        result = refresh.after_sync(self.cfg, self.db, limit=1)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(result["examined"], 1)
        self.assertEqual(result["refreshed"], 0)


class CanonRefreshCarryTest(_CarryFixture):
    def test_missing_designation_recovers_carried_verdicts_without_requests(self):
        self.freeze(self.repo.reviewed)
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        frozen = self.repo.merge_target()

        def unavailable(tx, milestone, threads):
            work.note(tx, self.lot, "Automatic reviewer designation unavailable at freeze")

        mark = work.milestone(self.db, self.lot, "frozen", sha=frozen,
                              actor="author", on_frozen=unavailable, after_commit=[])
        self.assertIsNone(act._effective_mark(self.db, self.lot, mark["id"]))
        self.db.execute("DELETE FROM agent_mailbox")
        result = refresh.after_sync(self.cfg, self.db)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(result["refreshed"], 1, result)
        effective = act._effective_mark(self.db, self.lot, mark["id"])
        self.assertIsNotNone(effective)
        self.assertEqual(effective[1], "carried")
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertEqual(self.review_mail(), [])

    def test_carried_designation_is_not_resent_by_the_refresh(self):
        """#7822 : un gel réaligné dont les deux verdicts sont reportés au gel
        même n'est pas redésigné par le contrôle du canon qui suit ; les
        sièges reportés ne reçoivent aucune demande (constat 4 de #225658 :
        #7745, #7767)."""
        self.freeze(self.repo.reviewed)
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        mark, _ = self.freeze(self.repo.merge_target())
        effective = act._effective_mark(self.db, self.lot, mark["id"])
        self.assertIsNotNone(effective)
        self.assertEqual(effective[1], "carried")
        self.db.execute("DELETE FROM agent_mailbox")
        result = refresh.after_sync(self.cfg, self.db)
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(self.review_mail(), [])
        self.assertEqual(self.designated(), ["rev1", "rev2"])
        self.assertEqual(act._effective_mark(self.db, self.lot, mark["id"])[1], "carried")


class RunnerRefreshTest(unittest.TestCase):
    def test_sync_recovery_rechecks_policy_before_capacity_retries(self):
        from ameesh import runner

        fake = types.SimpleNamespace(
            cfg=types.SimpleNamespace(canon="configured"), mediated=False,
            _designation_retry_ready=True)
        db = mock.MagicMock()
        order = []

        def record(name):
            def call(*args):
                order.append(name)
                return {}
            return call

        with mock.patch("ameesh.scoped_db.ScopedDb", return_value=db), \
                mock.patch.object(refresh, "after_sync", side_effect=record("policy")), \
                mock.patch.object(act, "retry_pending", side_effect=record("capacity")), \
                mock.patch.object(act, "reassign_unreachable", side_effect=record("hosts")):
            runner.Runner.retry_review_designations(fake)
        self.assertEqual(order, ["policy", "capacity", "hosts"])
        db.close.assert_called_once()
