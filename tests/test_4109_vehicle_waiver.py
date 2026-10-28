# SPDX-License-Identifier: AGPL-3.0-only
"""#4109 — un véhicule sans delta propre reçoit « verdict non requis » au gel.

Constat (paquets Nexlink 214 #4098, 215 #4107) : la désignation concluait
`review-designation-none … no changed path`, mais personne ne posait la
dispense #1979 ; les portes attendaient « verdict from a reviewer (none
designated) » jusqu'à ce qu'un agent se désigne à la main. Base réelle : le
gel, la désignation et la dispense s'écrivent dans la même transaction.
"""
from __future__ import annotations

from unittest import mock

from ameesh import packet_reviews
from ameesh import review_designation as rd
from ameesh import review_designation_activation as act
from ameesh import work

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture


class VehicleWaiverTest(_ActivationFixture, PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("UPDATE work_items SET branch = %s WHERE id = %s",
                        ("integration/paquet-4109", self.lot))

    def own(self, paths, commits):
        return mock.patch.object(packet_reviews, "designation_changes",
                                 return_value={"paths": paths, "commits": commits})

    def reviewers(self):
        return list(self.db.query("SELECT reviewers FROM work_items WHERE id = %s",
                                  (self.lot,))[0]["reviewers"] or [])

    def test_a_vehicle_without_own_delta_is_waived_in_the_freeze(self):
        with self.own([], []):
            _, out = self.freeze()
        self.assertEqual(out["state"], rd.NONE, out)
        self.assertEqual(out["why"], act.NO_PATH_WHY)
        self.assertEqual(self.reviewers(), ["-none"])
        self.assertEqual(len(self.notes(act.VEHICLE_WAIVER_EVENT)), 1)
        self.assertEqual(self.mailbox(), [])

    def test_the_waiver_is_written_once_on_a_refreeze_and_by_the_composer(self):
        with self.own([], []):
            self.freeze()
            self.freeze()
            act.designate(self.cfg, self.db, self.lot)
        # le composeur horaire redemande la dispense (#3845) : aucune seconde ligne
        work.waive_review(self.cfg, self.db, self.lot, actor="human:smichea")
        self.assertEqual(self.reviewers(), ["-none"])
        self.assertEqual(len(self.notes(act.VEHICLE_WAIVER_EVENT)), 1)
        self.assertEqual(len(self.notes("verdict not required")), 1)

    def test_a_vehicle_with_a_resolution_commit_is_not_waived(self):
        with self.own(["src/ameesh/mail.py"], [self.frozen]):
            _, out = self.freeze()
        self.assertNotEqual(out["state"], rd.NONE, out)
        self.assertNotIn("-none", self.reviewers())
        self.assertEqual(self.notes(act.VEHICLE_WAIVER_EVENT), [])

    def test_an_ordinary_lot_without_changed_path_is_not_waived(self):
        self.db.execute("UPDATE work_items SET branch = %s WHERE id = %s",
                        ("lot/4109-x", self.lot))
        self.repo.git("reset", "-q", "--hard", self.base)
        self.repo.git("commit", "-q", "--allow-empty", "-m", "no path")
        self.frozen = self.repo.git("rev-parse", "HEAD").strip()
        self.receipts()
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.NONE, out)
        self.assertEqual(out["why"], act.NO_PATH_WHY)
        self.assertNotIn("-none", self.reviewers())
        self.assertEqual(self.notes(act.VEHICLE_WAIVER_EVENT), [])
