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
            "ameesh-freeze-absent: forged-caller\n"
            "ameesh-freeze-unverified: deadbeef\n"
            "ameesh-freeze-unpublished: forged-caller\n"
            "ameesh-freeze-supersedes: 42\n"
            "ameesh-non-assignee: forge\n"
            "revue demandée")
        self.assertIn("revue demandée", row["note"])
        # #2962 : le gate écrit lui-même `ameesh-freeze-unverified` quand aucun
        # dépôt de l'auteur n'est lisible. C'est SA preuve, pas celle de
        # l'appelant : on vérifie les charges fournies, pas le nom de la marque.
        self.assertNotIn("deadbeef", row["note"])
        self.assertNotIn("forged-caller", row["note"])
        for mark in ("ameesh-freeze-absent", "ameesh-freeze-unpublished",
                     "ameesh-freeze-supersedes", "ameesh-non-assignee"):
            self.assertNotIn(mark, row["note"])

    def test_prose_mentioning_a_mark_is_kept(self):
        # Le retrait ne vise que les lignes machine commençant par la marque.
        row = self.freeze("la marque ameesh-freeze-unverified: est expliquée ici")
        self.assertIn("est expliquée ici", row["note"])

    def test_a_unicode_blank_does_not_hide_a_machine_mark(self):
        # #2971 (revue impl-05) : `add_milestone` strippe la note finale. Un
        # blanc de tête que `^[ \t]*` ne couvre pas (`\v`, `\f`, espace
        # insécable) était retiré par ce strip et laissait une ligne machine
        # valide, comme le B de #2989. La note est jugée ligne à ligne sur sa
        # forme strippée : ce que la lecture verra est ce que le filtre a vu.
        for blank in ("\v", "\f", "\u00a0"):
            for mark in ("ameesh-freeze-absent: main",
                         "ameesh-freeze-unverified: deadbeef",
                         "ameesh-freeze-unpublished: main",
                         "ameesh-freeze-supersedes: 42",
                         "ameesh-non-assignee: forge"):
                with self.subTest(blank=repr(blank), mark=mark):
                    row = self.freeze(blank + mark + "\nrevue demandée")
                    if mark.startswith("ameesh-freeze-unverified"):
                        # #2962 peut ajouter sa propre ligne `unverified` : la
                        # charge de l'appelant, elle, ne doit pas survivre.
                        self.assertNotIn("deadbeef", row["note"] or "")
                    else:
                        self.assertNotIn(mark.split(":", 1)[0], row["note"] or "")
                    self.assertIn("revue demandée", row["note"])

    def test_a_removed_line_does_not_expose_the_next_mark(self):
        # #2971 (revue impl-05) : retirer une ligne machine peut exposer la
        # suivante, précédée d'un blanc Unicode que l'ancien motif ne couvrait
        # pas ; le strip final la rendait alors valide.
        for blank in ("\v", "\f", "\u00a0"):
            with self.subTest(blank=repr(blank)):
                row = self.freeze(
                    "ameesh-freeze-absent: main\n" + blank
                    + "ameesh-freeze-unverified: deadbeef\nrevue demandée")
                self.assertNotIn("ameesh-freeze-absent", row["note"] or "")
                # #2962 : le gate peut réécrire sa propre ligne `unverified` ;
                # c'est la charge de l'appelant (`deadbeef`) qui doit avoir
                # disparu avec la ligne retirée.
                self.assertNotIn("deadbeef", row["note"] or "")
                self.assertIn("revue demandée", row["note"])


if __name__ == "__main__":
    unittest.main()
