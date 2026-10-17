# SPDX-License-Identifier: AGPL-3.0-only
"""#1318 : un verdict périmé ne désigne plus son auteur comme porte courante.

#705 désigne le relecteur à l'entrée en qa, mais gardait l'auteur du DERNIER
verdict en repli : après un regel (ou pour un verdict antérieur au dernier
gel), `work show` et la carte annonçaient « verdict by <auteur du verdict
périmé> » alors que cette revue était rendue et qu'aucun relecteur courant
n'était désigné (cas #804). Le repli disparaît : verdict courant -> son
auteur ; sinon le relecteur désigné ; sinon personne, donc « verdict from a
reviewer (none designated) ».
"""
from __future__ import annotations

import unittest

from ameesh import progress, stagnation


class VerdictPerimeTest(unittest.TestCase):
    """Le relecteur lu par `progress`, et l'attente rendue par `stagnation`."""

    def _item(self, **extra):
        item = {"state": "qa", "reviewer": "", "created_ts": 0.0}
        item.update(extra)
        return item

    def test_verdict_anterieur_au_gel_ne_designe_personne(self):
        # Cas #804 : GO cx1 du 13/10 17:49, gel du 14/10 01:25, aucune
        # désignation courante.
        rows = [
            {"kind": "frozen", "at_ts": 1.0},
            {"kind": "verdict", "at_ts": 2.0, "verdict": "ok", "actor": "ameesh-cx1"},
            {"kind": "frozen", "at_ts": 3.0},
        ]
        item = self._item()
        jalons = progress._work_item_milestones(item, [], [], rows)
        self.assertFalse(jalons["verdict_current"])
        self.assertIsNone(jalons["reviewer"])
        wait = stagnation.waiting(item, jalons, [])
        self.assertEqual(wait["what"], "verdict")
        self.assertIsNone(wait["who"])
        self.assertEqual(wait["label"], "verdict from a reviewer (none designated)")

    def test_regel_sans_designation_ne_nomme_pas_le_precedent(self):
        events = [
            {"state": "qa", "created_ts": 1.0},
            {"state": "build", "created_ts": 2.0, "actor": "relecteur"},
            {"state": "qa", "created_ts": 3.0},
        ]
        jalons = progress._work_item_milestones(self._item(), events, [])
        self.assertFalse(jalons["verdict_current"])
        self.assertIsNone(jalons["reviewer"])
        self.assertEqual(stagnation.waiting(self._item(), jalons, [])["label"],
                         "verdict from a reviewer (none designated)")

    def test_verdict_courant_garde_son_auteur(self):
        events = [
            {"state": "qa", "created_ts": 1.0},
            {"state": "build", "created_ts": 2.0, "actor": "relecteur"},
        ]
        jalons = progress._work_item_milestones(self._item(reviewer="designe"), events, [])
        self.assertTrue(jalons["verdict_current"])
        self.assertEqual(jalons["reviewer"], "relecteur")

    def test_designation_courante_est_nommee(self):
        rows = [
            {"kind": "frozen", "at_ts": 3.0},
            {"kind": "verdict", "at_ts": 2.0, "verdict": "ok", "actor": "ameesh-cx1"},
        ]
        item = self._item(reviewer="relecteur")
        jalons = progress._work_item_milestones(item, [], [], rows)
        self.assertFalse(jalons["verdict_current"])
        self.assertEqual(jalons["reviewer"], "relecteur")
        self.assertEqual(stagnation.waiting(item, jalons, [])["label"],
                         "verdict by relecteur")


if __name__ == "__main__":
    unittest.main()
