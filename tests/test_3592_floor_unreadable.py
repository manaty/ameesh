# SPDX-License-Identifier: AGPL-3.0-only
"""#3592 — journal du plancher illisible : la complétion le dit, sans écrire.

Constat (revue #3557, dev2) : `_floor_of` rendait 0 sur un journal illisible ;
avec une marque `designated`, `designate` rendait cette marque sans passer par
`prepare`, donc la complétion du plancher était sautée sans trace.

Témoins : une marque désignée et un journal du plancher illisible → `designate`
(et `reviewers --apply`) rend `unavailable` en nommant la lecture en échec,
sans nouvelle marque, sans courrier, et la colonne des relecteurs est
inchangée ; le chemin lisible reste couvert par #3557.
"""
from __future__ import annotations

import dataclasses
import os
from unittest import mock

from ameesh import review_designation as rd
from ameesh import review_designation_activation as act
from ameesh import review_floor, work
from ameesh.work_reviewers import designated

from . import test_2447_review_designation_activation as base
from .support import PgTestCase


class FloorUnreadableTest(base._ActivationFixture, PgTestCase):
    """Le gel réel, le canon réel, puis un journal de plancher illisible."""

    def setUp(self):
        super().setUp()
        self.canon = base._Canon(
            os.path.join(self.tmp, "canon-un-avis-floor-3592"),
            federation=base.FEDERATION.replace("            reviews: 2",
                                               "            reviews: 1"))
        self.cfg = dataclasses.replace(self.cfg, canon=self.canon.root,
                                       canon_ref="HEAD")

    def test_an_unreadable_floor_does_not_return_the_mark_silently(self):
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        marks_before = list(self.marks())
        mail_before = len(self.mailbox())
        column = designated(work.get(self.db, self.lot))

        with mock.patch.object(review_floor, "of_lot",
                               side_effect=RuntimeError("journal illisible")):
            result = act.designate(self.cfg, self.db, self.lot)

        self.assertEqual(result["state"], "unavailable", result)
        self.assertIn("review floor unreadable", result["why"])
        self.assertIn("RuntimeError", result["why"])
        self.assertEqual(list(self.marks()), marks_before)
        self.assertEqual(len(self.mailbox()), mail_before)
        self.assertEqual(designated(work.get(self.db, self.lot)), column)

    def test_reviewers_apply_reports_the_unreadable_floor(self):
        self.freeze()
        with mock.patch.object(review_floor, "of_lot",
                               side_effect=RuntimeError("journal illisible")):
            out = act.reviewers(self.cfg, self.db, self.lot, apply=True)

        self.assertEqual(out["applied"]["state"], "unavailable", out)
        self.assertIn("unreadable", out["applied"]["why"])
