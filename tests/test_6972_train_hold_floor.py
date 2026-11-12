# SPDX-License-Identifier: AGPL-3.0-only
"""#6972 : l'enrôlement et l'admission du train appliquent les refus de
`paquet-candidats` — hold de pilote (#3375) et plancher de relecture (#3183) —
par la même lecture (`packet_candidates._pilot_holds`, `review_floor.apply`).

Constat : plan nexlink 3618d1be avec #6336 (hold de pilote) et #6912
(plancher 2, un seul avis acquis).
"""
import types
from unittest import mock

from ameesh import (packet_candidates, registry, review_floor, storage, train, train_admissions,
                    train_candidates, work)
from . import test_1467_train_admissions as base_test
from .support import PgTestCase

BASE, HEAD = base_test.BASE, base_test.HEAD


class _Fixture:
    """La vraie file d'admission de #1467 (`TrainAdmissionsTest`), sans
    rejouer ses tests ici."""

    enqueue = base_test.TrainAdmissionsTest.enqueue
    qualify = base_test.TrainAdmissionsTest.qualify

    def setUp(self):
        PgTestCase.setUp(self)
        self.project = types.SimpleNamespace(name="ameesh", repo=self.tmp, target="main")
        self.ops = storage.of(self.db).work
        self.history = mock.Mock(db=self.db, path=self.tmp)
        self.history.qualify.side_effect = lambda row, base, **kw: dict(
            head=row["frozen_sha"], required=kw["required"])
        self.requirement = mock.Mock(side_effect=lambda item, row, base, path:
            train_admissions.Requirement(row["work_item_id"], base, row["frozen_sha"],
                                         "sensitive", 2, "c" * 40,
                                         files=({"path": "content", "class": "sensitive",
                                                 "rule": ["content"]},)))
        self.admissions = train_admissions.Admissions(
            self.db, self.project, self.history, policy=self.requirement)
        registry.upsert(self.db, "author", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp, status="idle")
        self.entry = self.enqueue()

    @property
    def lot(self):
        return self.entry["work_item_id"]

    def light(self):
        """Un barème léger à un avis : seul le plancher peut exiger le second."""
        self.requirement.side_effect = lambda item, row, base, path: \
            train_admissions.Requirement(
                row["work_item_id"], base, row["frozen_sha"], "light", 1, "c" * 40,
                files=({"path": "docs/x.md", "class": "light", "rule": None},))


class TrainPilotHoldTest(_Fixture, PgTestCase):
    def test_held_lot_is_not_admitted_then_admitted_once_lifted(self):
        work.note(self.db, self.lot, "hold: consigne owner, réveil demain",
                  actor="human:smichea")
        with self.assertRaisesRegex(train.MemberDeferred, "pilot-hold: hold pilote"):
            self.qualify()
        self.history.qualify.assert_not_called()
        work.note(self.db, self.lot, "unhold: consigne levée", actor="human:smichea")
        self.assertEqual(self.qualify()["members"][0]["policy"]["lot"], self.lot)

    def test_held_lot_can_still_be_quarantined(self):
        work.note(self.db, self.lot, "hold: attente", actor="human:smichea")
        self.admissions._current(self.entry, {self.lot: self.entry})

    def test_shared_reader_is_paquet_candidats_and_unreadable_refuses(self):
        with mock.patch.object(packet_candidates, "_pilot_holds",
                               side_effect=packet_candidates.CandidateError("registry")):
            with self.assertRaisesRegex(train.MemberDeferred, "registry"):
                self.qualify()

    def test_inventory_names_the_hold_and_drops_it_once_lifted(self):
        candidate = mock.Mock(return_value={"sha": HEAD, "causes": []})
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (self.lot,))
        work.note(self.db, self.lot, "hold: témoin", actor="human:smichea")

        def causes():
            inventory = train_candidates.Inventory(self.db, self.project,
                                                   candidate=candidate).read()
            return next(row["causes"] for row in inventory["candidates"]
                        if row["work_item_id"] == self.lot)
        self.assertTrue(any(cause.startswith("pilot-hold:") for cause in causes()))
        work.note(self.db, self.lot, "unhold: levé", actor="human:smichea")
        self.assertFalse(any(cause.startswith("pilot-hold:") for cause in causes()))


class TrainReviewFloorTest(_Fixture, PgTestCase):
    def test_floor_raises_the_admission_count_and_its_lift_lowers_it(self):
        self.light()
        self.assertEqual(self.qualify()["members"][0]["policy"]["reviews"], 1)
        review_floor.set_floor(self.cfg, self.db, self.lot, 2, reason="deux familles",
                               actor="human:smichea", complete=False)
        member = self.qualify()["members"][0]
        self.assertEqual((member["policy"]["reviews"], member["policy"]["reviews_floor"]),
                         (2, 2))
        _, kwargs = self.history.qualify.call_args
        self.assertEqual((kwargs["required"], kwargs["klass"]["reviews_floor"]), (2, 2))
        review_floor.set_floor(self.cfg, self.db, self.lot, 0, reason="levé",
                               actor="human:smichea", complete=False)
        self.assertEqual(self.qualify()["members"][0]["policy"]["reviews"], 1)

    def test_floor_not_reached_defers_the_member(self):
        self.light()
        review_floor.set_floor(self.cfg, self.db, self.lot, 2, reason="deux familles",
                               actor="human:smichea", complete=False)
        self.history.qualify.side_effect = lambda row, base, **kw: (
            (_ for _ in ()).throw(train.TrainError("%d review(s) required" % kw["required"]))
            if kw["required"] > 1 else dict(head=row["frozen_sha"]))
        with self.assertRaisesRegex(train.MemberDeferred, "2 review"):
            self.qualify()

    def test_unreadable_floor_requires_two_reviews(self):
        self.light()
        with mock.patch.object(storage.of(self.db).work.__class__, "events_noted",
                               side_effect=RuntimeError("journal")):
            member = self.qualify()["members"][0]
        self.assertEqual(member["policy"]["reviews"], 2)
        self.assertIn("journal", member["policy"]["reviews_floor_unreadable"])

    def test_preparation_reads_the_same_floor(self):
        self.light()
        review_floor.set_floor(self.cfg, self.db, self.lot, 2, reason="deux familles",
                               actor="human:smichea", complete=False)
        requirement = train_admissions.requirement_of(
            self.db, self.requirement, work.get(self.db, self.lot),
            dict(work_item_id=self.lot, frozen_sha=HEAD), BASE, self.tmp)
        self.assertEqual((requirement.reviews, requirement.klass()["reviews_floor"]), (2, 2))
