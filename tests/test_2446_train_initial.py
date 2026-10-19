# SPDX-License-Identifier: AGPL-3.0-only
"""#2446 : la frontière d'activation qualifie l'absence d'historique de file.

Un gel né après l'instant où le train devient l'unique écrivain du paquet, et
sans reçu d'allocation ni de retrait, est une absence initiale. Un gel plus
ancien, ou un reçu, laisse la décision au chemin normal : jamais de supposition.
"""
from __future__ import annotations

import unittest

from ameesh import storage, train, train_initial, work
from .support import PgTestCase


class BoundaryTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.ops = storage.of(self.db).work
        self.item = work.add(self.db, title="initial fixture", app="ameesh",
                             source="fixture", actor="fixture")
        self.mark = self.ops.add_milestone(self.item["id"], "frozen",
                                           sha="a" * 40, actor="fixture",
                                           verdict=None, note="")
        self.subject = dict(work_item_id=self.item["id"], frozen_id=self.mark["id"])

    def age(self, seconds):
        self.db.execute(
            "UPDATE work_item_milestones SET at = clock_timestamp()"
            " - %s * interval '1 second' WHERE id = %s",
            (int(seconds), self.mark["id"]))

    def now(self):
        return self.db.query("SELECT extract(epoch FROM clock_timestamp())::float8"
                             " AS now")[0]["now"]

    def test_freeze_after_the_boundary_without_receipt_is_initial(self):
        self.age(10)
        boundary = train_initial.Boundary(self.now() - 60)
        self.assertTrue(boundary(self.db, subject=self.subject))

    def test_freeze_before_the_boundary_stays_unknown(self):
        self.age(3600)
        boundary = train_initial.Boundary(self.now() - 60)
        self.assertFalse(boundary(self.db, subject=self.subject))

    def test_any_receipt_leaves_the_decision_to_the_normal_path(self):
        self.age(10)
        boundary = train_initial.Boundary(self.now() - 60)
        self.db.execute(
            "INSERT INTO integration_train_queue_receipts"
            " (work_item_id, generation, frozen_id, kind) VALUES (%s, 1, %s, 'removed')",
            (self.item["id"], self.mark["id"]))
        self.assertFalse(boundary(self.db, subject=self.subject))

    def test_missing_freeze_or_subject_refuses(self):
        boundary = train_initial.Boundary(self.now() - 60)
        broken = dict(self.subject, frozen_id=self.mark["id"] + 1000)
        with self.assertRaisesRegex(train.TrainError, "frozen milestone"):
            boundary(self.db, subject=broken)
        for broken in ({}, dict(work_item_id=0, frozen_id=1),
                       dict(work_item_id=self.item["id"], frozen_id="1")):
            with self.subTest(broken=broken), self.assertRaises(train.TrainError):
                boundary(self.db, subject=broken)

    def test_activation_boundary_must_be_an_exact_instant(self):
        for value in (None, True, "now", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(train.TrainError):
                train_initial.Boundary(value)


if __name__ == "__main__":
    unittest.main()
