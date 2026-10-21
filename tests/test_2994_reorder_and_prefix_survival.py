# SPDX-License-Identifier: AGPL-3.0-only
"""#2994 — ni un réordre dans le même fichier ni un préfixe ne prouvent la survie.

Cas réel #2614 : la tête b6b97f08 (réécrite par dev4 depuis main) gardait dev3
(deepseek) pour auteur par son commit 43b171323. Avec la preuve de #2977, le
commit restait « survivant » pour deux raisons : (1) des lignes de fixture
retirées puis rajoutées dans le même fichier passaient pour « déplacées » et
échappaient au filtre hérité ; (2) la recherche dans l'arbre (B8) acceptait une
ligne plus longue qui contient la ligne cherchée.

Témoins : (1) réordre dans le même fichier + lignes propres retirées : non
survivant ; (2) ligne propre retrouvée seulement comme préfixe : non
survivant ; (3) ligne propre exacte reportée ailleurs : survivant (B8) ;
(4) déplacement entre chemins : toujours une preuve ; (5) réordre seul :
survivant tant que la tête garde un de ses hunks tel quel ; (6) diff
binaire ou arbre illisible : survivant.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from unittest import TestCase, mock

from ameesh import review_authors

from .test_2447_review_designation_activation import _Repo

FIXTURE = ("def test_pick():\n"
           "    worker._turn_launched = True\n"
           "    worker._consume_scheduled_wake(spec)\n")
REORDERED = ("def test_pick():\n"
             "    worker._consume_scheduled_wake(spec)\n"
             "    worker._turn_launched = True\n")


class CommitSurvivesTest(TestCase):
    """La preuve de contenu, commit par commit, sans base de données."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2994-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))

    def survives(self, head, sha, base=""):
        return review_authors._commit_survives(
            self.repo.root, head, sha, {}, base)

    def test_a_same_file_reorder_does_not_keep_a_rewritten_corrector(self):
        # Le correctif réordonne la fixture et ajoute ses lignes propres ; la
        # tête repart de la base avec la fixture d'origine, sans ses lignes.
        self.repo.write("tests/test_pick.py", FIXTURE)
        base = self.repo.commit("base")
        self.repo.write("tests/test_pick.py",
                        REORDERED + "    assert own_corrector_guard(worker)\n")
        fix = self.repo.commit("fix", agent="dev3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.write("tests/test_pick.py",
                        FIXTURE + "    assert rewritten_by_author(worker)\n")
        head = self.repo.commit("impl: réécriture", agent="dev4")
        self.assertFalse(self.survives(head, fix, base))

    def test_a_prefix_in_the_head_is_not_the_own_line(self):
        # La ligne propre n'est plus que le début d'une ligne plus longue,
        # ailleurs dans la tête : ce n'est pas sa trace.
        self.repo.write("a.py", "base_line_here()\n")
        base = self.repo.commit("base")
        self.repo.write("b.py", "self.assertIsNone(self.pick(worker),\n")
        fix = self.repo.commit("fix", agent="dev3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.write("b.py", "rewritten_by_author()\n")
        self.repo.write("c.py",
                        "self.assertIsNone(self.pick(worker), 'raison')\n")
        head = self.repo.commit("impl: réécriture", agent="dev4")
        self.assertFalse(review_authors._any_line_in_head(
            self.repo.root, head, ["self.assertIsNone(self.pick(worker),"]))
        self.assertFalse(self.survives(head, fix, base))

    def test_an_exact_own_line_reported_elsewhere_keeps_the_corrector(self):
        # B8 : la ligne propre, à l'identique, vit sous un autre chemin.
        self.repo.write("a.py", "base_line_here()\n")
        base = self.repo.commit("base")
        self.repo.write("b.py", "    own_corrector_guard(worker)\n")
        fix = self.repo.commit("fix", agent="dev3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.write("b.py", "rewritten_by_author()\n")
        self.repo.write("c.py", "def moved():\n    own_corrector_guard(worker)\n")
        head = self.repo.commit("impl: déplacement", agent="dev4")
        self.assertTrue(self.survives(head, fix, base))

    def test_a_move_between_paths_still_proves_the_correction(self):
        # Le correctif déplace la fixture d'un fichier à l'autre : la ligne
        # retirée d'un chemin et ajoutée à un autre reste une preuve.
        self.repo.write("tests/test_pick.py", FIXTURE)
        base = self.repo.commit("base")
        self.repo.write("tests/test_pick.py", "")
        self.repo.write("tests/test_moved.py", FIXTURE)
        fix = self.repo.commit("fix", agent="dev3")
        self.assertTrue(self.survives(fix, fix, base))

    def test_a_same_file_reorder_alone_keeps_the_corrector(self):
        # Sans ligne propre, le réordre se prouve par son hunk tel quel : tant
        # que la tête le garde, l'auteur est conservé.
        self.repo.write("tests/test_pick.py", FIXTURE)
        base = self.repo.commit("base")
        self.repo.write("tests/test_pick.py", REORDERED)
        fix = self.repo.commit("fix", agent="dev3")
        self.assertTrue(self.survives(fix, fix, base))
        self.repo.write("tests/test_other.py", "unrelated_author_line()\n")
        head = self.repo.commit("impl: autre fichier", agent="dev4")
        self.assertTrue(self.survives(head, fix, base))

    def test_a_reverted_reorder_does_not_keep_the_corrector(self):
        # La tête rétablit l'ordre d'origine : le réordre n'y subsiste plus.
        self.repo.write("tests/test_pick.py", FIXTURE)
        base = self.repo.commit("base")
        self.repo.write("tests/test_pick.py", REORDERED)
        fix = self.repo.commit("fix", agent="dev3")
        self.repo.write("tests/test_pick.py", FIXTURE)
        head = self.repo.commit("impl: ordre rétabli", agent="dev4")
        self.assertFalse(self.survives(head, fix, base))

    def squash(self, base, files):
        """Tête réécrite par l'auteur : squash depuis la base de `files`, plus
        un autre fichier modifié (cas de relecture cx2, #2994 B1)."""
        self.repo.git("reset", "-q", "--hard", base)
        for path, text in files.items():
            self.repo.write(path, text)
        self.repo.write("docs/note.md", "autre fichier modifié\n")
        return self.repo.commit("impl: squash", agent="dev4")

    def test_a_kept_reindentation_keeps_the_corrector(self):
        # B1 (a) : le correctif indente une ligne sous le `if` ; la ligne
        # dépouillée et ses voisines sont celles du parent.
        parent = "if condition_here():\n    first_action()\nsecond_action()\n"
        fixed = "if condition_here():\n    first_action()\n    second_action()\n"
        self.repo.write("a.py", parent)
        base = self.repo.commit("base")
        self.repo.write("a.py", fixed)
        fix = self.repo.commit("fix", agent="cx3")
        head = self.squash(base, {"a.py": fixed})
        self.assertTrue(self.survives(head, fix, base))
        head = self.squash(base, {"a.py": parent})
        self.assertFalse(self.survives(head, fix, base))

    def test_a_kept_reorder_of_repeated_lines_keeps_the_corrector(self):
        # B1 (b) : même comptes, mêmes paires de lignes voisines.
        names = {"A": "alpha_line()", "B": "beta_line()", "C": "gamma_line()"}
        parent = "".join(names[key] + "\n" for key in "ABACABACA")
        fixed = "".join(names[key] + "\n" for key in "ACABABACA")
        self.repo.write("a.py", parent)
        base = self.repo.commit("base")
        self.repo.write("a.py", fixed)
        fix = self.repo.commit("fix", agent="cx3")
        head = self.squash(base, {"a.py": fixed})
        self.assertTrue(self.survives(head, fix, base))

    def test_a_binary_diff_keeps_the_corrector(self):
        self.repo.write("a.py", "base_line_here()\n")
        base = self.repo.commit("base")
        with open(os.path.join(self.repo.root, "blob.bin"), "wb") as handle:
            handle.write(b"\x00\x01own binary\x00")
        fix = self.repo.commit("fix", agent="dev3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.write("a.py", "rewritten_by_author()\n")
        head = self.repo.commit("impl: réécriture", agent="dev4")
        self.assertTrue(self.survives(head, fix, base))

    def test_an_unreadable_head_tree_keeps_the_corrector(self):
        failed = mock.Mock(returncode=128, stdout=b"")
        with mock.patch.object(review_authors.plan_git, "_git",
                               return_value=failed):
            self.assertTrue(review_authors._any_line_in_head(
                self.repo.root, "deadbeef", ["own_corrector_guard(worker)"]))
        with mock.patch.object(review_authors.plan_git, "_git",
                               side_effect=OSError("git absent")):
            self.assertTrue(review_authors._any_line_in_head(
                self.repo.root, "deadbeef", ["own_corrector_guard(worker)"]))
