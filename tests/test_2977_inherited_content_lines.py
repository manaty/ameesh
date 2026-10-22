# SPDX-License-Identifier: AGPL-3.0-only
"""#2977 — une ligne déjà présente avant le correctif ne prouve pas sa survie.

Cas réel #2968 (gel #15335) : le correctif dev2 `2cd06510`, retiré par
l'auteur, ajoutait `of_lot.assert_not_called()`, ligne déjà présente sur main
dans `tests/test_1104`. La preuve de contenu de #2924 la retrouvait dans
l'arbre de la tête (B8) et gardait claude famille d'auteur.

Témoins : (1) correctif retiré dont une ligne existe déjà dans la base (même
chemin, autre chemin, ou base de fusion avancée) : auteur non repris ; (2) même
ligne, propre au correctif et reportée ailleurs dans la tête : auteur repris
(B8) ; (3) un déplacement reste une preuve ; les témoins #2924 sont inchangés.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from unittest import TestCase

from ameesh import merge_queue, review_authors, work

from .test_2447_review_designation_activation import _Repo
from .test_2924_history_rewrite_provenance import _RewriteFixture

LINE = "    of_lot.assert_not_called()\n"


class _InheritedLineFixture(_RewriteFixture):
    """Base, auteur deepseek, un correctif codex unique (gel A)."""

    #: `tests/test_1104.py` de la base
    BASE_TESTS = "def test_1104():\n" + LINE
    #: fichiers écrits par le correctif codex, en plus de runner.py
    FIX_FILES = {"tests/test_runner.py": "def test_admission():\n" + LINE}

    def _build_head_a(self):
        self.repo.write("src/ameesh/runner.py", "base\n")
        self.repo.write("tests/test_1104.py", self.BASE_TESTS)
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/runner.py", "base\nlot\n")
        self.author = self.repo.commit("impl: lot", agent="impl")
        self.receipt("impl", "deepseek", [self.author])
        self.repo.write("src/ameesh/runner.py",
                        "base\nlot\n"
                        "if not admitted:\n"
                        "    return deferred  # cx3 fix\n")
        for path, text in self.FIX_FILES.items():
            self.repo.write(path, text)
        self.fix = self.repo.commit("cx3: garde d'admission", agent="cx3")
        self.receipt("cx3", "codex", [self.fix])
        self.head_a = self.fix
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.freeze(self.head_a)

    def rewrite(self, start, files):
        """Tête réécrite par l'auteur depuis `start` : les lignes propres de
        cx3 n'y sont plus, son fichier de test existe encore (réécrit)."""
        self.repo.git("reset", "--hard", start)
        self.repo.write("src/ameesh/runner.py",
                        "base\nlot\n"
                        "def _interrupt_admitted():\n"
                        "    return rewrite_is_admitted()  # impl rewrite\n")
        self.repo.write("tests/test_runner.py",
                        "def test_rewrite():\n"
                        "    assert rewrite_is_admitted()\n")
        for path, text in files.items():
            self.repo.write(path, text)
        head = self.repo.commit("impl: corrections réécrites", agent="impl")
        self.receipt("impl", "deepseek", [head])
        freeze = self.freeze(head)
        milestone = {"id": int(freeze["id"]), "sha": head, "actor": "impl",
                     "at_ts": freeze.get("at_ts")}
        return milestone, head

    def names(self, milestone, head, base):
        authors, unknown, correctors = self.reading(
            milestone, [head], base, head)
        self.assertEqual(unknown, [])
        return ({row["name"] for row in authors},
                {row["name"] for row in correctors})


class LineAlreadyInBaseTest(_InheritedLineFixture):
    """(1) La ligne existait dans la base, sous un autre chemin (#2968)."""

    def test_the_withdrawn_correction_is_not_carried(self):
        milestone, head = self.rewrite(self.base, {})
        self.assertEqual(self.names(milestone, head, self.base),
                         ({"impl"}, set()))

    def test_the_two_families_can_review_the_rewritten_head(self):
        milestone, head = self.rewrite(self.base, {})
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/runner.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/runner.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        for actor in ("cx3", "dev1"):
            self.verdict(actor, head)
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), head,
            repo=self.repo.root, klass=klass, item=self.lot_item(), required=2)
        self.assertFalse(out.get("error"), out)
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual({row["actor"] for row in out["accepted"]},
                         {"cx3", "dev1"})

    def test_a_surviving_own_line_still_keeps_the_corrector(self):
        # #2924 inchangé : une seule ligne propre encore présente suffit.
        milestone, head = self.rewrite(
            self.base, {"src/ameesh/runner.py":
                        "base\nlot\n    return deferred  # cx3 fix\n"})
        self.assertEqual(self.names(milestone, head, self.base),
                         ({"impl", "cx3"}, {"cx3"}))


class LineAlreadyInSamePathTest(_InheritedLineFixture):
    """(1) Le correctif ajoutait la ligne au fichier qui la portait déjà."""

    FIX_FILES = {"tests/test_runner.py": "def test_admission():\n"
                                         "    assert admitted() is False\n",
                 "tests/test_1104.py": "def test_1104():\n" + LINE
                                       + "def test_1104_guard():\n" + LINE}

    def test_the_base_occurrence_does_not_carry_the_corrector(self):
        milestone, head = self.rewrite(self.base, {})
        self.assertEqual(self.names(milestone, head, self.base),
                         ({"impl"}, set()))


class LineInAdvancedTargetTest(_InheritedLineFixture):
    """(1) La ligne est entrée dans la cible après le correctif : la base de
    fusion de la tête et de la cible la porte, elle ne prouve rien."""

    BASE_TESTS = "def test_1104():\n    assert lot_is_quiet()\n"

    def test_the_merge_base_occurrence_does_not_carry_the_corrector(self):
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("tests/test_1104.py", "def test_1104():\n" + LINE)
        target = self.repo.commit("main: témoin 1104")
        milestone, head = self.rewrite(target, {})
        self.assertEqual(self.names(milestone, head, target),
                         ({"impl"}, set()))


class OwnLineMovedElsewhereTest(_InheritedLineFixture):
    """(2) La ligne est propre au correctif : reportée ailleurs, elle le garde
    (#2924 B8)."""

    BASE_TESTS = "def test_1104():\n    assert lot_is_quiet()\n"

    def test_the_moved_own_line_keeps_the_corrector(self):
        milestone, head = self.rewrite(
            self.base, {"tests/test_elsewhere.py": "def test_moved():\n" + LINE})
        self.assertEqual(self.names(milestone, head, self.base),
                         ({"impl", "cx3"}, {"cx3"}))


class CommitSurvivesTest(TestCase):
    """La preuve de contenu, commit par commit, sans base de données."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2977-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))

    def survives(self, head, sha, base=""):
        return review_authors._commit_survives(
            self.repo.root, head, sha, {}, base)

    def test_a_longer_base_line_does_not_hide_an_own_line(self):
        # `git grep -F` trouve les lignes qui contiennent le motif ; seule une
        # ligne égale (à l'indentation près) est héritée.
        self.repo.write("a.py", "value = guard_admission(lot) + 1\n")
        base = self.repo.commit("base")
        self.repo.write("b.py", "guard_admission(lot)\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.assertEqual(review_authors._lines_in_tree(
            self.repo.root, base, ["guard_admission(lot)"]), set())
        self.assertTrue(self.survives(fix, fix, base))

    def test_a_moved_base_line_still_proves_the_move(self):
        # Le correctif déplace une ligne de la base : la ligne retirée d'un
        # côté et ajoutée de l'autre reste une preuve, comme avant #2977.
        self.repo.write("a.py", "keep_this_line_here()\n")
        self.repo.write("b.py", "header_of_b_file()\n")
        base = self.repo.commit("base")
        self.repo.write("a.py", "")
        self.repo.write("b.py", "header_of_b_file()\nkeep_this_line_here()\n"
                                "own_corrector_line()\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.write("b.py", "header_of_b_file()\nkeep_this_line_here()\n")
        head = self.repo.commit("impl: ligne propre retirée", agent="impl")
        self.assertTrue(self.survives(head, fix, base))

    def test_only_inherited_additions_fall_back_to_the_removed_lines(self):
        # Le correctif ne fait que recopier une ligne de la base et retirer une
        # ligne : la lecture passe aux lignes retirées.
        self.repo.write("a.py", "existing_call_here()\nremoved_by_fix()\n")
        base = self.repo.commit("base")
        self.repo.write("a.py", "existing_call_here()\n")
        self.repo.write("b.py", "existing_call_here()\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.assertTrue(self.survives(fix, fix, base))
        self.repo.write("a.py", "existing_call_here()\nremoved_by_fix()\n")
        head = self.repo.commit("impl: retrait annulé", agent="impl")
        # la copie de b.py subsiste à son chemin : le correctif aussi
        self.assertTrue(self.survives(head, fix, base))
        self.repo.write("b.py", "")
        head = self.repo.commit("impl: copie retirée", agent="impl")
        self.assertFalse(self.survives(head, fix, base))

    def test_a_withdrawn_copy_of_a_base_line_is_not_carried(self):
        # B1 (cx2) : le correctif ne fait que copier une ligne de la base ; la
        # tête réécrit b.py sans elle. L'original de a.py ne prouve rien.
        self.repo.write("a.py", "existing_call_here()\n")
        base = self.repo.commit("base")
        self.repo.write("b.py", "existing_call_here()\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.write("b.py", "rewritten_call_here()\n")
        head = self.repo.commit("impl: b.py réécrit", agent="impl")
        self.assertFalse(self.survives(head, fix, base))

    def test_a_copy_still_at_its_path_keeps_the_corrector(self):
        self.repo.write("a.py", "existing_call_here()\n")
        base = self.repo.commit("base")
        self.repo.write("b.py", "existing_call_here()\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.write("c.py", "unrelated_author_line()\n")
        head = self.repo.commit("impl: autre fichier", agent="impl")
        self.assertTrue(self.survives(head, fix, base))

    def test_a_copy_whose_path_is_gone_keeps_the_corrector(self):
        # Chemin absent : déplacement possible, le doute conserve l'auteur.
        self.repo.write("a.py", "existing_call_here()\n")
        base = self.repo.commit("base")
        self.repo.write("b.py", "existing_call_here()\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.write("c.py", "rewritten_call_here()\n")
        head = self.repo.commit("impl: b.py absent", agent="impl")
        self.assertTrue(self.survives(head, fix, base))

    def test_a_duplicate_in_the_same_file_is_not_carried(self):
        # B2 (impl-04) : seule addition, une ligne déjà dans le même fichier ;
        # la tête retire la copie. L'original au même chemin ne prouve rien.
        self.repo.write("a.py", "def test_a():\n" + LINE)
        base = self.repo.commit("base")
        self.repo.write("a.py", "def test_a():\n" + LINE + LINE)
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.write("c.py", "rewritten_call_here()\n")
        head = self.repo.commit("impl: autre fichier", agent="impl")
        self.assertFalse(self.survives(head, fix, base))

    def test_a_duplicate_still_in_the_same_file_keeps_the_corrector(self):
        self.repo.write("a.py", "def test_a():\n" + LINE)
        base = self.repo.commit("base")
        self.repo.write("a.py", "def test_a():\n" + LINE + LINE)
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.write("c.py", "unrelated_author_line()\n")
        head = self.repo.commit("impl: autre fichier", agent="impl")
        self.assertTrue(self.survives(head, fix, base))

    def test_a_diff_without_meaningful_lines_keeps_the_corrector(self):
        self.repo.write("a.py", "existing_call_here()\n")
        base = self.repo.commit("base")
        self.repo.write("b.py", "pass\n}\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.write("b.py", "rewritten_call_here()\n")
        head = self.repo.commit("impl: b.py réécrit", agent="impl")
        self.assertTrue(self.survives(head, fix, base))

    def test_an_unreadable_parent_excludes_nothing(self):
        # Un commit racine n'a pas de parent : rien n'est hérité, la ligne
        # prouve la survie (le doute conserve l'auteur).
        self.repo.write("a.py", "root_line_of_corrector()\n")
        root = self.repo.commit("root", agent="cx3")
        self.assertEqual(review_authors._lines_in_tree(
            self.repo.root, root + "^", ["root_line_of_corrector()"]), set())
        self.assertTrue(self.survives(root, root))
