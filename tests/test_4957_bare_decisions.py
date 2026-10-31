# SPDX-License-Identifier: AGPL-3.0-only
"""#4957 : `decisions` fonctionne au profil nu (sans module dev).

Le noyau générique tient et rend un lot via les transitions du workflow de
développement ; au profil nu (L98 T2b, CI/tests) cette table n'existe pas.
La demande reste enregistrée, la note seule est journalisée et l'état du lot
ne bouge pas — sans ValueError.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from tests import support  # noqa: F401  (isole la config de l'hôte)
from tests.support import PgTestCase
from ameesh import decisions, work


class BareProfileDecisionsTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE work_items CASCADE")
        patcher = mock.patch.dict(os.environ, {"AMEESH_MODULES": "bare"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lot = work.add(self.db, title="profil nu")["id"]

    def test_hold_lot_only_notes_without_the_dev_workflow(self):
        item = work.get(self.db, self.lot)
        self.assertEqual(
            decisions._hold_lot(self.db, item, note="decision #1 requested",
                                actor="orch"),
            "intake")
        self.assertEqual(work.get(self.db, self.lot)["state"], "intake")
        notes = [event["note"] for event in work.events(self.db, self.lot)]
        self.assertIn("decision #1 requested", notes)

    def test_release_lot_only_notes_without_the_dev_workflow(self):
        self.db.execute("UPDATE work_items SET state = 'waiting_human' WHERE id = %s",
                        (self.lot,))
        view = {"id": 1, "lot": str(self.lot), "previous_state": "build"}
        out = decisions._release_lot(self.db, view, actor="human:alice",
                                     note="decision #1 answered")
        self.assertEqual((out["state"], out["moved"]), ("waiting_human", False))
        self.assertEqual(work.get(self.db, self.lot)["state"], "waiting_human")
        notes = [event["note"] for event in work.events(self.db, self.lot)]
        self.assertTrue(any("decision #1 answered" in note
                            and "stays in waiting_human" in note for note in notes),
                        notes)


if __name__ == "__main__":
    unittest.main()
