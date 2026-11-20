# SPDX-License-Identifier: AGPL-3.0-only
"""#2908: a packet carrier (`integration/**`) is never a member of a train packet.

The shadow plans of 20/10 23:00 and 21/10 00:00 on ameesh-agents-2 listed the
carrier of h-2200 (#2891) as a member next to the lot it carried; the packet
composer already refuses it (`packet-vehicle`), the train did not.
"""
import types
import unittest
from unittest import mock

from ameesh import registry, storage, train, train_admissions, train_candidates, work
from .support import PgTestCase

BASE, HEAD = "a" * 40, "b" * 40
CARRIER = "integration/paquet-h-20261020-2200"


class InventoryPacketVehicleTest(unittest.TestCase):
    def setUp(self):
        self.project = types.SimpleNamespace(name="ameesh", repo="/unused", target="main")
        self.ops = mock.Mock()
        self.ops.milestones.return_value = [dict(id=1, kind="frozen", sha=HEAD)]
        self.reader = mock.Mock(side_effect=lambda marks, waived: dict(
            sha=HEAD, origin="frozen", declared=marks[0], verdict=None, notes=[], causes=[]))
        patch = mock.patch.object(storage, "of", return_value=types.SimpleNamespace(work=self.ops))
        patch.start()
        self.addCleanup(patch.stop)
        source = mock.patch.object(train_candidates.merge_queue, "source_of",
                                   return_value="github.com/manaty/ameesh")
        source.start()
        self.addCleanup(source.stop)
        self.inventory = train_candidates.Inventory(None, self.project, candidate=self.reader)

    def row(self, lot, branch):
        return dict(id=lot, app="ameesh", state="qa", branch=branch,
                    branch_target="origin/main", type="evolution", title="lot")

    def test_carrier_is_named_packet_vehicle_and_its_unit_stays_a_candidate(self):
        self.ops.items.return_value = [self.row(2891, CARRIER),
                                       self.row(2889, "lot/2889-oauth-interrupted")]
        rows = {row["work_item_id"]: row for row in self.inventory.read()["candidates"]}
        self.assertEqual(rows[2891]["causes"], ["packet-vehicle"])
        self.assertEqual(rows[2889]["causes"], [])

    def test_train_branch_is_a_vehicle_too_and_missing_branch_is_not_doubled(self):
        self.ops.items.return_value = [self.row(1, "integration/train-20261021-1100-abc"),
                                       self.row(2, "")]
        rows = {row["work_item_id"]: row for row in self.inventory.read()["candidates"]}
        self.assertEqual(rows[1]["causes"], ["packet-vehicle"])
        self.assertEqual(rows[2]["causes"], ["missing-branch"])


class AdmissionPacketVehicleTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.project = types.SimpleNamespace(name="vehicle-test", repo=self.tmp, target="main")
        self.ops = storage.of(self.db).work
        self.history = mock.Mock(db=self.db, path=self.tmp)
        self.history.qualify.side_effect = lambda row, base, **kw: dict(
            head=row["frozen_sha"], required=kw["required"])
        policy = mock.Mock(side_effect=lambda item, row, base, path:
            train_admissions.Requirement(row["work_item_id"], base, row["frozen_sha"],
                                         "light", 0, "c" * 40,
                                         files=({"path": "tests/x.py", "class": "light",
                                                 "rule": ["tests"]},)))
        self.admissions = train_admissions.Admissions(
            self.db, self.project, self.history, policy=policy)
        registry.upsert(self.db, "author", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp, status="idle")

    def enqueue(self, branch):
        item = work.add(self.db, title="queued", app=self.project.name, source="fixture",
                        actor="fixture", assignee="author", branch=branch, branch_target="main")
        freeze = self.ops.add_milestone(item["id"], "frozen", sha=HEAD, actor="author",
                                         verdict=None, note="")
        return self.ops.queue_add(item["id"], target="main", frozen_sha=HEAD, note="",
                                  actor="fixture", source=self.admissions.source,
                                  frozen_id=freeze["id"])

    def test_queued_carrier_is_deferred_as_packet_vehicle(self):
        carrier = self.enqueue(CARRIER)
        with self.assertRaisesRegex(train.MemberDeferred, "packet-vehicle"):
            self.admissions.qualify([carrier], BASE, "packet")
        self.history.qualify.assert_not_called()

    def test_queued_work_branch_still_qualifies(self):
        unit = self.enqueue("lot/unit")
        proof = self.admissions.qualify([unit], BASE, "packet")
        self.assertEqual(proof["members"][0]["admission"], train.identity(unit))


if __name__ == "__main__":
    unittest.main()
