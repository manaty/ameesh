# SPDX-License-Identifier: AGPL-3.0-only
"""#1924 : snapshot reel lu pour les verificateurs C (tables natives).

Aucune base : `storage.of` est remplace par un faux magasin ; le lecteur ne
doit lire que les tables natives, ne rien ecrire, et ne declarer que les
collections qu'il collecte reellement.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import process_snapshot
from ameesh.process_snapshot import READ_SOURCES, read_snapshot
from ameesh.process_verifiers import Command, verify, verify_hold

REV = "a" * 40
OTHER = "b" * 40
COMMAND_ID = "11111111-1111-4111-8111-111111111111"


def item(**overrides) -> dict:
    values = {"id": 7, "type": "evolution", "title": "lot temoin",
              "state": "build", "assignee": "agent:dev1",
              "reviewer": "agent:reviewer", "loops": 0, "closed_at": None,
              "workflow_ref": "canon@f12867b7:workflows/acme.json",
              "workflow_stage": "work", "workflow_version": 3}
    values.update(overrides)
    return values


class _Work:
    def __init__(self, item, milestones=(), events=()):
        self._item = item
        self._milestones = list(milestones)
        self._events = list(events)

    def get(self, item_id):
        return self._item

    def milestones(self, item_id, limit):
        return list(self._milestones)

    def events(self, item_id, limit):
        return list(self._events)


class _Decisions:
    def __init__(self, pending=()):
        self._pending = list(pending)

    def pending_for_lot(self, lot):
        return list(self._pending)


class SnapshotReelTest(unittest.TestCase):

    def read(self, *, item=None, milestones=(), events=(), pending=()):
        store = SimpleNamespace(work=_Work(item, milestones, events),
                                decisions=_Decisions(pending))
        patch = mock.patch.object(process_snapshot, "storage",
                                  SimpleNamespace(of=lambda db: store))
        with patch:
            return read_snapshot(object(), 7)

    def command(self, guard, **overrides) -> Command:
        values = dict(work_item_id=7, command_id=COMMAND_ID, guard=guard,
                      from_stage="work", to_stage="next", expected_version=3,
                      actor="agent:dev1", source="cli", references={})
        values.update(overrides)
        return Command(**values)

    def test_lot_inconnu_rend_none(self):
        self.assertIsNone(self.read(item=None))

    def test_snapshot_lit_le_lot_les_jalons_et_les_decisions(self):
        snapshot = self.read(
            item=item(),
            milestones=(
                {"kind": "frozen", "sha": REV, "actor": "agent:dev1"},
                {"kind": "delivered", "sha": REV, "actor": "human:smichea"},
            ),
            events=({"id": 1, "state": "build"},),
            pending=({"id": 11}, {"id": 12}))
        self.assertEqual(snapshot.workflow_ref,
                         "canon@f12867b7:workflows/acme.json")
        self.assertEqual(snapshot.workflow_stage, "work")
        self.assertEqual(snapshot.workflow_version, 3)
        self.assertEqual(snapshot.revision, REV)
        self.assertEqual(snapshot.decisions_pending, 2)
        self.assertEqual(snapshot.events, ({"id": 1, "state": "build"},))
        self.assertEqual(snapshot.sources, READ_SOURCES)
        self.assertEqual(snapshot.deliveries, (
            {"id": "delivered:%s" % REV, "sha": REV, "accepted": True,
             "actor": "human:smichea"},))

    def test_revision_gel_puis_fusion_puis_livraison(self):
        frozen_and_merged = self.read(
            item=item(),
            milestones=({"kind": "merged", "sha": OTHER},
                        {"kind": "frozen", "sha": REV}))
        self.assertEqual(frozen_and_merged.revision, REV)
        merged = self.read(item=item(),
                           milestones=({"kind": "merged", "sha": OTHER},))
        self.assertEqual(merged.revision, OTHER)
        delivered = self.read(item=item(),
                              milestones=({"kind": "delivered", "sha": REV},))
        self.assertEqual(delivered.revision, REV)
        self.assertEqual(self.read(item=item()).revision, "")

    def test_lot_sans_workflow_inchange_puis_raccord(self):
        without = self.read(item=item(workflow_ref="", workflow_stage="",
                                      workflow_version=0))
        decision = verify(self.command("start_work"), without)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, "workflow-absent")
        decision = verify(self.command("start_work"), self.read(item=item()))
        self.assertTrue(decision.accepted, decision)

    def test_gardes_de_deploiement_refusent_sans_source(self):
        decision = verify(
            self.command("staging_pass", references={"staging": "dep-1"}),
            self.read(item=item()))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, "source-unavailable")
        self.assertIn("deployments", decision.message)

    def test_decisions_pendantes_alimentent_waiting_human(self):
        decision = verify_hold("waiting_human", self.read(item=item()))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, "hold-no-decision")
        pending = self.read(item=item(), pending=({"id": 11},))
        self.assertTrue(verify_hold("waiting_human", pending).accepted)

    def test_completion_code_sur_livraison_native(self):
        reference = {"delivery": "delivered:%s" % REV}
        snapshot = self.read(item=item(),
                             milestones=({"kind": "delivered", "sha": REV},))
        decision = verify(self.command("work_completed",
                                       references=reference), snapshot)
        self.assertTrue(decision.accepted, decision)
        decision = verify(self.command("work_completed",
                                       references=reference),
                          self.read(item=item(),
                                    milestones=({"kind": "frozen",
                                                 "sha": REV},)))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, "completion-evidence-missing")
        decision = verify(self.command("work_completed",
                                       references=reference),
                          self.read(item=item()))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, "revision-missing")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
