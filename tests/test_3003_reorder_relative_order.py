# SPDX-License-Identifier: AGPL-3.0-only
"""#3003 — un réordre qui subsiste avec une ligne insérée garde son auteur.

Constat de relecture #2994 (deepseek, gel 26b3faba) : base `A,B`, correctif
`B,A`, tête `B,X,A`. Le réordre subsiste, mais le hunk du correctif (`B,A`
d'un seul tenant) ne se retrouve plus dans la tête : `_hunks_survive` rendait
False et le correcteur perdait son auteur.

Témoins : (1) réordre gardé, ligne insérée entre les deux : survivant ;
(2) ordre rétabli, ligne insérée : non survivant ; (3) bloc déplacé au-delà
d'une fonction, puis la tête insère au milieu : survivant ; (4) lignes
répétées : la preuve d'ordre ne décide pas, le hunk tel quel reste la
preuve ; (5) les témoins de #2994 (réordre seul, ordre rétabli, réécriture
#2614) sont inchangés (leur fichier de tests).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from unittest import TestCase

from ameesh import review_authors

from .test_2447_review_designation_activation import _Repo

A = "    worker._turn_launched = True\n"
B = "    worker._consume_scheduled_wake(spec)\n"
X = "    worker.record_inserted_step(spec)\n"
HEADER = "def test_pick():\n"


class OrderSurvivesTest(TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3003-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))

    def survives(self, head, sha, base=""):
        return review_authors._commit_survives(
            self.repo.root, head, sha, {}, base)

    def reorder(self, parent, fixed):
        self.repo.write("tests/test_pick.py", parent)
        base = self.repo.commit("base")
        self.repo.write("tests/test_pick.py", fixed)
        fix = self.repo.commit("fix", agent="dev3")
        return base, fix

    def head(self, text):
        self.repo.write("tests/test_pick.py", text)
        self.repo.write("docs/note.md", "autre fichier modifié\n")
        return self.repo.commit("impl: suite", agent="dev4")

    def test_a_kept_reorder_with_an_inserted_line_keeps_the_corrector(self):
        base, fix = self.reorder(HEADER + A + B, HEADER + B + A)
        head = self.head(HEADER + B + X + A)
        self.assertTrue(self.survives(head, fix, base))

    def test_a_reverted_reorder_with_an_inserted_line_drops_the_corrector(self):
        base, fix = self.reorder(HEADER + A + B, HEADER + B + A)
        head = self.head(HEADER + A + X + B)
        self.assertFalse(self.survives(head, fix, base))

    def test_a_block_moved_past_a_function_survives_an_insertion(self):
        first = ("def first_helper():\n"
                 "    return compute_first_value()\n")
        second = ("def second_helper():\n"
                  "    return compute_second_value()\n")
        base, fix = self.reorder(first + second, second + first)
        head = self.head(second + "def inserted_helper():\n"
                         "    return compute_inserted_value()\n" + first)
        self.assertTrue(self.survives(head, fix, base))
        head = self.head(first + "def inserted_helper():\n"
                         "    return compute_inserted_value()\n" + second)
        self.assertFalse(self.survives(head, fix, base))

    def test_repeated_lines_fall_back_to_the_kept_hunk(self):
        # Une ligne répétée n'a pas de position unique : l'ordre relatif ne
        # décide pas, le hunk tel quel reste la preuve (#2994).
        parent = HEADER + A + B + A + B
        fixed = HEADER + B + A + A + B
        base, fix = self.reorder(parent, fixed)
        self.assertIsNone(review_authors._order_survives(
            self.repo.root, fix, fix, "tests/test_pick.py", [A, B], {}))
        self.assertTrue(self.survives(self.head(fixed), fix, base))
        self.assertFalse(self.survives(self.head(parent), fix, base))

    def test_an_unreadable_path_is_undecided(self):
        base, fix = self.reorder(HEADER + A + B, HEADER + B + A)
        self.assertIsNone(review_authors._order_survives(
            self.repo.root, fix, fix, "absent.py", [A], {}))
