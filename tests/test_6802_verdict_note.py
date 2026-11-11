# SPDX-License-Identifier: AGPL-3.0-only
"""#6802 — un OK sans note sur un lot sensible, posé dans un tour, est refusé.

Constat (nexlink, 11/11) : deux OK de claude1 sur des lots SQL sensibles
(#6792, #6666) portaient une note vide. Le reçu (#1465) désignait un tour
réel de claude1 (session dédiée à un autre lot) qui avait bien relu les
diffs, mais n'avait mis ses constats que dans son rapport de tour ; la
session suivante de claude1, sans mémoire de ce tour, ne pouvait pas les
confirmer. La note est la seule trace de lecture qui survit à la session.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import lot_class, review_provenance, work

from .support import PgTestCase

FROZEN = "4537fb3bcd36" + "0" * 28


class VerdictNoteTest(PgTestCase):

    def setUp(self):
        super().setUp()
        self.lot = work.add(self.db, title="un lot SQL", app="ameesh",
                            actor="auteur")["id"]
        work.move(self.db, self.lot, "build")
        work.milestone(self.db, self.lot, "frozen", sha=FROZEN, actor="auteur")

    def verdict(self, *, note="", klass=lot_class.SENSITIVE, turn="t-6802"):
        env = {"AMEESH_TURN_ID": turn}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(lot_class, "of_lot",
                                  return_value={"class": klass}), \
                mock.patch.object(review_provenance, "proof_for_current_turn",
                                  return_value=None):
            return work.milestone(self.db, self.lot, "verdict", sha=FROZEN,
                                  actor="relecteur", verdict="ok", note=note)

    def verdicts(self):
        return [m for m in work.milestones(self.db, self.lot)
                if m["kind"] == "verdict"]

    def test_un_ok_vide_sur_un_lot_sensible_est_refuse_dans_un_tour(self):
        for klass in (lot_class.SENSITIVE, lot_class.RELEASE):
            with self.assertRaises(work.WorkError) as caught:
                self.verdict(klass=klass)
            self.assertIn("note-missing", str(caught.exception))
            self.assertIn("--note", str(caught.exception))
        self.assertEqual(self.verdicts(), [])

    def test_une_classe_illisible_vaut_sensible(self):
        with mock.patch.dict(os.environ, {"AMEESH_TURN_ID": "t"}, clear=False), \
                mock.patch.object(lot_class, "of_lot", side_effect=RuntimeError), \
                mock.patch.object(review_provenance, "proof_for_current_turn",
                                  return_value=None):
            with self.assertRaises(work.WorkError) as caught:
                work.milestone(self.db, self.lot, "verdict", sha=FROZEN,
                               actor="relecteur", verdict="ok")
        self.assertIn("note-missing", str(caught.exception))

    def test_une_note_faite_seulement_de_blancs_ne_compte_pas(self):
        with self.assertRaises(work.WorkError):
            self.verdict(note="  \n\t ")
        self.assertEqual(self.verdicts(), [])

    def test_un_ok_note_est_ecrit_avec_sa_note(self):
        self.verdict(note="tête %s relue : 774 ajoute retired_at IS NULL" % FROZEN[:12])
        [row] = self.verdicts()
        self.assertEqual(row["verdict"], "ok")
        self.assertIn("retired_at", row["note"])

    def test_un_lot_leger_garde_l_ok_sans_note(self):
        self.verdict(klass=lot_class.LIGHT)
        self.assertEqual(len(self.verdicts()), 1)

    def test_hors_tour_l_ok_sans_note_reste_permis(self):
        # orchestrateur, humain : hors tour managé, rien ne change
        with mock.patch.dict(os.environ, {"AMEESH_TURN_ID": ""}, clear=False):
            work.milestone(self.db, self.lot, "verdict", sha=FROZEN,
                           actor="human:smichea", verdict="ok")
        self.assertEqual(len(self.verdicts()), 1)

    def test_un_blocage_sans_note_n_est_pas_concerne(self):
        with mock.patch.dict(os.environ, {"AMEESH_TURN_ID": "t"}, clear=False), \
                mock.patch.object(review_provenance, "proof_for_current_turn",
                                  return_value=None):
            work.milestone(self.db, self.lot, "verdict", sha=FROZEN,
                           actor="relecteur", verdict="blocked")
        self.assertEqual(self.verdicts()[0]["verdict"], "blocked")


if __name__ == "__main__":
    unittest.main()
