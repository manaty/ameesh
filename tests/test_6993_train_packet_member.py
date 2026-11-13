# SPDX-License-Identifier: AGPL-3.0-only
"""#6993: un lot déjà composé dans un paquet ouvert n'a qu'une voie.

Constat du 11/11 (orchestrateur #213180) : #6973 et #6976 étaient membres du
paquet 569, composé à la main et enregistré à 20:34Z ; l'inventaire du train
les a pourtant inscrits individuellement à 20:36Z. L'enrôlement et la
préparation lisent désormais la liaison `work_packet_members` écrite par
`work packet record` (#2443) : un lot porté par un paquet ouvert est refusé
(`packet-member`), jamais inscrit une seconde fois.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import storage, train, train_candidates, work
from .support import PgTestCase

BASE, HEAD = "a" * 40, "b" * 40
PACKET = "integration/paquet-6993"


class InventoryPacketMemberTest(unittest.TestCase):
    """L'inventaire, sur une lecture de stockage simulée."""

    def setUp(self):
        self.project = types.SimpleNamespace(name="ameesh", repo="/unused", target="main")
        self.ops = mock.Mock()
        self.ops.milestones.return_value = [dict(id=1, kind="frozen", sha=HEAD)]
        self.ops.packet_members.return_value = []
        self.reader = mock.Mock(side_effect=lambda marks, waived: dict(
            sha=HEAD, origin="frozen", declared=marks[0], verdict=None, notes=[],
            causes=[]))
        patch = mock.patch.object(storage, "of", return_value=types.SimpleNamespace(
            work=self.ops,
            progress=types.SimpleNamespace(lot_hold_events=lambda ids, patterns: [])))
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

    def member(self, lot, *, package=3001, state="qa", branch=PACKET):
        return dict(package_id=package, work_item_id=lot, package_state=state,
                    package_branch=branch, carrier_branch=branch,
                    source_sha=HEAD, frozen_sha=HEAD)

    def causes(self):
        return {row["work_item_id"]: row["causes"]
                for row in self.inventory.read()["candidates"]}

    def test_open_packet_member_is_named_and_its_carrier_stays_a_vehicle(self):
        self.ops.items.return_value = [self.row(3001, PACKET),
                                       self.row(3002, "lot/unit")]
        self.ops.packet_members.return_value = [self.member(3002)]
        rows = self.causes()
        self.assertEqual(rows[3001], ["packet-vehicle"])
        self.assertEqual(len(rows[3002]), 1)
        self.assertTrue(rows[3002][0].startswith("packet-member: #3001 %s" % PACKET))
        self.assertIn("landing path", rows[3002][0])
        self.ops.packet_members.assert_called_once_with(item_ids=[3001, 3002])

    def test_completed_carrier_and_non_packet_branch_do_not_carry(self):
        self.ops.items.return_value = [self.row(3002, "lot/unit")]
        self.ops.packet_members.return_value = [
            self.member(3002, state="delivered"),
            self.member(3002, package=3003, state="closed", branch="lot/not-a-packet")]
        self.assertEqual(self.causes()[3002], [])

    def test_unreadable_or_malformed_membership_refuses_the_list(self):
        self.ops.items.return_value = [self.row(3002, "lot/unit")]
        self.ops.packet_members.side_effect = RuntimeError("packet table down")
        with self.assertRaisesRegex(train.TrainError, "packet table down"):
            self.inventory.read()
        self.ops.packet_members.side_effect = None
        self.ops.packet_members.return_value = [{"package_id": 3001}]
        with self.assertRaisesRegex(train.TrainError, "malformed packet membership"):
            self.inventory.read()

    def test_empty_inventory_does_not_read_memberships(self):
        self.ops.items.return_value = []
        self.assertEqual(self.inventory.read()["candidates"], [])
        self.ops.packet_members.assert_not_called()


class PacketMemberPreparationTest(PgTestCase):
    """La liaison réelle (Postgres), puis le refus de la préparation."""

    def setUp(self):
        super().setUp()
        self.project = types.SimpleNamespace(name="packet-member-test", repo=self.tmp,
                                             target="main")
        self.ops = storage.of(self.db).work
        self.unit = self.lot("unit", "lot/unit")
        self.carrier = self.lot("paquet", PACKET)
        # Le gel est écrit au stockage : la CLI `work.milestone` résoudrait le
        # SHA dans un clone (et son empreinte), sans rapport avec ce témoin.
        self.freeze = self.ops.add_milestone(self.unit["id"], "frozen", sha=HEAD,
                                             actor="author", verdict=None, note="")
        self.record([self.unit["id"]])
        self.reader = mock.Mock(return_value=dict(
            sha=HEAD, origin="frozen", causes=[]))

    def lot(self, title, branch):
        item = work.add(self.db, title=title, app=self.project.name, source="fixture",
                        actor="fixture", assignee="human:fixture", branch=branch,
                        branch_target="main")
        for state in ("build", "qa"):
            work.move(self.db, item["id"], state)
        return item

    def record(self, units, *, carrier=None):
        storage.of(self.db).work.record_packet(
            int((carrier or self.carrier)["id"]), branch=PACKET, base_sha=BASE,
            head_sha=HEAD,
            units=[{"work_item_id": int(lot), "unit_sha": "c" * 40,
                    "source_sha": HEAD, "frozen_sha": HEAD} for lot in units],
            note="packet composition recorded", actor="orch")

    def inventory(self):
        return train_candidates.Inventory(self.db, self.project,
                                          candidate=self.reader).read()

    def preparation(self):
        repository = types.SimpleNamespace(
            project=self.project, clone=types.SimpleNamespace(path=self.tmp))
        history = mock.Mock(db=self.db, path=self.tmp)
        return train_candidates.Preparation(
            self.db, self.project, repository=repository, history=history,
            policy=mock.Mock()), history

    def candidate(self):
        return dict(work_item_id=self.unit["id"], branch="lot/unit", target="main",
                    causes=[], candidate=dict(
                        sha=HEAD, origin="frozen",
                        declared=dict(kind="frozen", id=self.freeze["id"], sha=HEAD)))

    def causes(self):
        return {row["work_item_id"]: row["causes"]
                for row in self.inventory()["candidates"]}

    def test_inventory_names_the_packet_and_preparation_rechecks_the_link(self):
        rows = self.causes()
        self.assertEqual(rows[self.carrier["id"]], ["packet-vehicle"])
        self.assertEqual(len(rows[self.unit["id"]]), 1)
        self.assertTrue(rows[self.unit["id"]][0].startswith(
            "packet-member: #%s %s" % (self.carrier["id"], PACKET)))
        preparation, history = self.preparation()
        with self.assertRaisesRegex(train.TrainError, "packet-member"):
            preparation.prepare(self.candidate(), BASE)
        # Le refus tombe avant toute qualification historique du dépôt.
        history.qualify.assert_not_called()

    def test_a_closed_carrier_releases_its_units(self):
        # Un porteur abandonné ne porte plus : le lot reprend sa voie propre.
        self.db.execute("UPDATE work_items SET state = 'closed',"
                        " close_reason = 'abandoned' WHERE id = %s",
                        (self.carrier["id"],))
        self.assertEqual(self.causes()[self.unit["id"]], [])

    def test_a_recomposition_without_the_unit_releases_it(self):
        self.record([])
        self.assertEqual(self.causes()[self.unit["id"]], [])


if __name__ == "__main__":
    unittest.main()
