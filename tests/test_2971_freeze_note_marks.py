# SPDX-License-Identifier: AGPL-3.0-only
"""#2971 — `--note` ne fournit aucune preuve machine de gel.

#2786 retirait déjà `ameesh-freeze-absent:` de la note d'appelant ; #2962 a
ajouté `ameesh-freeze-unverified:` et `ameesh-freeze-unpublished:`, #2989
`ameesh-freeze-supersedes:`. Une ligne machine fournie par `--note` doit être
retirée comme `absent`, sinon l'appelant fabrique une preuve que le gate n'a
pas produite.
"""
from __future__ import annotations

import unittest

from ameesh import work

from .support import PgTestCase

HEAD = "a" * 40


class FreezeNoteMarksTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.lot = work.add(self.db, title="un lot", app="ameesh",
                            actor="auteur")["id"]
        work.move(self.db, self.lot, "build")

    def freeze(self, note):
        return work.milestone(self.db, self.lot, "frozen", sha=HEAD,
                              actor="auteur", note=note)

    def test_machine_marks_in_a_caller_note_are_removed(self):
        row = self.freeze(
            "ameesh-freeze-absent: main\n"
            "ameesh-freeze-unverified: deadbeef\n"
            "ameesh-freeze-unpublished: main\n"
            "ameesh-freeze-supersedes: 42\n"
            "revue demandée")
        self.assertIn("revue demandée", row["note"])
        for mark in ("ameesh-freeze-absent", "ameesh-freeze-unverified",
                     "ameesh-freeze-unpublished", "ameesh-freeze-supersedes"):
            self.assertNotIn(mark, row["note"])

    def test_prose_mentioning_a_mark_is_kept(self):
        # Le retrait ne vise que les lignes machine commençant par la marque.
        row = self.freeze("la marque ameesh-freeze-unverified: est expliquée ici")
        self.assertIn("est expliquée ici", row["note"])


if __name__ == "__main__":
    unittest.main()
