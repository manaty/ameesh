# SPDX-License-Identifier: AGPL-3.0-only
"""#3220 B8 × #2994 — le retrait d'un contenu que la base n'a jamais reçu.

#2994 (cas #2614) : la tête réécrite depuis `main` garde l'auteur d'un gel
antérieur parce que des lignes de fixture du commit de relecture existent
encore dans la tête. Deux pièges sont fermés ici, sans rouvrir #3216 :

* un réordre dans le même fichier n'est pas un déplacement : ses lignes sont
  héritées, et leur simple compte dans la tête ne prouve pas la copie ;
* quand les ajouts propres du commit ont tous disparu, un retrait ne fonde la
  survie que si la base de fusion de la tête et de la cible portait déjà la
  ligne **au même chemin ou au chemin renommé `R`**, et si le hunk est un
  retrait pur (pas le côté retiré d'une modification dont les ajouts ont
  disparu). #3220 garde son témoin inverse : un retrait pur d'une ligne de la
  base subsiste et garde l'auteur.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from unittest import TestCase

from ameesh import plan_git, review_authors

from .support import REPO
from .test_2447_review_designation_activation import _Repo

#: témoins réels #2614 (gel #14804) : la tête réécrite depuis main et le commit
#: de relecture dev3 ; la base est main au moment du constat. Absents d'un
#: clone partiel : le test est alors sauté (les témoins synthétiques restent).
REAL_HEAD = "b6b97f080099fbf035939dfa425fa8ace0b57e41"
REAL_REVIEW = "43b171323f0ae08fcf327fe7a42384638785528c"
REAL_BASE = "5cb7b7fb2d8715c32e7594d33ba220c406e87020"

FIXTURE = ("def test_pick():\n"
           "    worker._turn_launched = True\n"
           "    worker._consume_scheduled_wake(spec)\n")
REORDERED = ("def test_pick():\n"
             "    worker._consume_scheduled_wake(spec)\n"
             "    worker._turn_launched = True\n")


class CommitSurvivesTest(TestCase):
    """La preuve de contenu, commit par commit, sans base de données."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3220-2994-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))

    def survives(self, head, sha, base=""):
        return review_authors._commit_survives(
            self.repo.root, head, sha, {}, base)

    def test_a_reorder_copy_does_not_keep_a_rewritten_corrector(self):
        # Cas #2614 synthétique : le correctif réordonne la fixture, en copie
        # une ligne (compte +1 dans la tête par coïncidence) et ajoute une
        # ligne propre ; la tête repart de la base sans ses ajouts.
        path = "tests/test_pick.py"
        self.repo.write(path, FIXTURE)
        base = self.repo.commit("base")
        self.repo.write(path, FIXTURE + "    lot_only_line()\n")
        self.repo.commit("lot: contenu antérieur")
        self.repo.write(path, REORDERED + "    worker._turn_launched = True\n"
                                        + "    assert own_guard(worker)\n")
        fix = self.repo.commit("fix", agent="dev3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.write(path, FIXTURE + "    worker._turn_launched = True\n"
                                        + "    assert rewritten(worker)\n")
        head = self.repo.commit("impl: réécriture", agent="dev4")
        self.assertFalse(self.survives(head, fix, base))

    def test_a_pure_removal_of_a_base_line_keeps_the_corrector(self):
        # Témoin #3220 : la ligne retirée existe dans la base au même chemin,
        # le hunk est un retrait pur, le retrait subsiste.
        self.repo.write("a.py", "keep_line_here()\nold_guard_line()\n")
        base = self.repo.commit("base")
        self.repo.write("a.py", "keep_line_here()\n")
        self.repo.write("docs/review.md", "review own line\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.write("a.py", "keep_line_here()\n")
        head = self.repo.commit("impl: retrait conservé", agent="impl")
        self.assertTrue(self.survives(head, fix, base))

    def test_a_removed_lot_only_line_does_not_keep_the_corrector(self):
        # La ligne a existé dans le lot mais jamais dans la base de fusion :
        # son absence de la tête ne prouve pas que le retrait a survécu.
        self.repo.write("a.py", "keep_line_here()\n")
        base = self.repo.commit("base")
        self.repo.write("a.py", "keep_line_here()\nlot_only_line()\n")
        self.repo.commit("lot: contenu antérieur")
        self.repo.write("a.py", "keep_line_here()\n")
        self.repo.write("docs/review.md", "review own line\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.write("docs/other.md", "main line\n")
        head = self.repo.commit("impl: base seule", agent="impl")
        self.assertFalse(self.survives(head, fix, base))

    def test_a_replaced_line_does_not_keep_the_corrector(self):
        # La ligne retirée vient de la base, mais le hunk la remplaçait par
        # une ligne propre qui a disparu : ce n'est pas un retrait pur.
        self.repo.write("a.py", "keep_line_here()\nold_guard_line()\n")
        base = self.repo.commit("base")
        self.repo.write("a.py", "keep_line_here()\nnew_guard_line()\n")
        self.repo.write("docs/review.md", "review own line\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.write("a.py", "keep_line_here()\n")
        head = self.repo.commit("impl: remplacement réécrit", agent="impl")
        self.assertFalse(self.survives(head, fix, base))

    def test_a_pure_removal_of_a_renamed_base_line_keeps_the_corrector(self):
        # mesh-design : la base de fusion porte la ligne au chemin renommé
        # (`R`), pas seulement au même chemin.
        self.repo.write("old.py", "keep_line_here()\n"
                                   "second_context_line()\n"
                                   "third_context_line()\n"
                                   "old_guard_line()\n")
        base = self.repo.commit("base")
        self.repo.git("mv", "old.py", "new.py")
        self.repo.commit("lot: renommage")
        self.repo.write("new.py", "keep_line_here()\n"
                                  "second_context_line()\n"
                                  "third_context_line()\n")
        self.repo.write("docs/review.md", "review own line\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.git("mv", "old.py", "new.py")
        self.repo.write("new.py", "keep_line_here()\nrewritten_context()\n")
        head = self.repo.commit("impl: renommage enrichi", agent="impl")
        self.assertTrue(self.survives(head, fix, base))

    def test_the_real_2614_witness_does_not_survive(self):
        # Témoin réel de #2994 : la tête b6b97f08 réécrite par dev4 depuis
        # main ne reprend pas le commit de relecture dev3 43b171323.
        for sha in (REAL_HEAD, REAL_REVIEW, REAL_BASE):
            if not plan_git.local_commit(REPO, sha):
                self.skipTest("témoins #2614 absents de ce clone")
        self.assertFalse(review_authors._commit_survives(
            REPO, REAL_HEAD, REAL_REVIEW, {}, REAL_BASE))

    def test_a_surviving_removal_block_precedes_a_copied_heading(self):
        # B1 (cx3/auditeur) : le doc du correctif porte une en-tête héritée,
        # donc `copied` n'est pas vide ; un retrait survivant dans un autre
        # fichier ne doit pas être conclu « disparu » par cette branche.
        context = "".join("context_line_%d()\n" % index
                          for index in range(1, 7))
        self.repo.write("gate.py",
                        "allow_all_users_without_check()\n" + context)
        self.repo.write("docs/other.md", "shared review heading\n")
        base = self.repo.commit("base")
        self.repo.write("gate.py", context)
        self.repo.write("docs/review.md", "shared review heading\n"
                                         "own review line\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.git("rm", "-q", "gate.py")
        self.repo.write("relocated/gate.py", context + "rewritten_rest()\n")
        self.repo.write("docs/review.md", "rewritten doc line\n")
        head = self.repo.commit("impl: retrait regroupé", agent="impl")
        self.assertTrue(self.survives(head, fix, base))

    def test_a_common_removed_line_is_not_an_annulment(self):
        # B9 (dev2) : « return None » figure déjà dans le fichier de reprise
        # avant le correctif ; sa présence ne prouve pas que le retrait a été
        # annulé. Le compte d'occurrences le dit.
        self.repo.write("gate.py",
                        "def corrected():\n"
                        "    return None\n"
                        "    guard_line_1()\n"
                        "    guard_line_2()\n"
                        "    guard_line_3()\n"
                        "    guard_line_4()\n")
        self.repo.write("relocated/gate.py",
                        "def old_feature():\n"
                        "    return None\n"
                        "    old_line()\n")
        base = self.repo.commit("base")
        self.repo.write("gate.py",
                        "def corrected():\n"
                        "    guard_line_1()\n"
                        "    guard_line_2()\n"
                        "    guard_line_3()\n"
                        "    guard_line_4()\n")
        self.repo.write("docs/review.md", "own review line\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.git("rm", "-q", "gate.py")
        self.repo.write("relocated/gate.py",
                        "def corrected():\n"
                        "    guard_line_1()\n"
                        "    guard_line_2()\n"
                        "    guard_line_3()\n"
                        "    guard_line_4()\n"
                        "def old_feature():\n"
                        "    return None\n"
                        "    old_line()\n")
        head = self.repo.commit("impl: contenu regroupé", agent="impl")
        self.assertTrue(self.survives(head, fix, base))

    def test_a_common_removed_line_in_an_independent_function_is_not_an_annulment(self):
        # B10 (revues cx2 + cx3) : `return None` ajouté dans une fonction
        # indépendante ne réintroduit pas la garde retirée. Le compte global
        # d'occurrences le prenait pour une annulation et retirait l'auteur à
        # tort ; seule la pré-image localisée du hunk prouve une réintroduction.
        self.repo.write("gate.py",
                        "def corrected():\n"
                        "    return None\n"
                        "    guard_line_1()\n"
                        "    guard_line_2()\n"
                        "    guard_line_3()\n"
                        "    guard_line_4()\n")
        self.repo.write("relocated/gate.py",
                        "def old_feature():\n"
                        "    return None\n"
                        "    old_line()\n")
        base = self.repo.commit("base")
        self.repo.write("gate.py",
                        "def corrected():\n"
                        "    guard_line_1()\n"
                        "    guard_line_2()\n"
                        "    guard_line_3()\n"
                        "    guard_line_4()\n")
        self.repo.write("docs/review.md", "own review line\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.git("rm", "-q", "gate.py")
        self.repo.write("relocated/gate.py",
                        "def corrected():\n"
                        "    guard_line_1()\n"
                        "    guard_line_2()\n"
                        "    guard_line_3()\n"
                        "    guard_line_4()\n"
                        "def old_feature():\n"
                        "    return None\n"
                        "    old_line()\n"
                        "def unrelated_feature():\n"
                        "    return None\n")
        head = self.repo.commit("impl: contenu regroupé", agent="impl")
        self.assertTrue(self.survives(head, fix, base))

    def test_a_duplicate_pre_image_is_not_an_annulment(self):
        # B11 (revues cx2, cx3, auditeur) : la pré-image du retrait existe déjà
        # intacte, dans la base, au fichier de reprise. Sa seule présence ne
        # prouve pas que le retrait a été annulé ; seule une occurrence de plus
        # que dans le même fichier au commit corrigé le prouve.
        pre = ("    shared_before_1()\n"
               "    shared_before_2()\n"
               "    shared_before_3()\n"
               "    return None\n"
               "    shared_after_1()\n"
               "    shared_after_2()\n"
               "    shared_after_3()\n")
        self.repo.write("gate.py", "def corrected():\n" + pre)
        self.repo.write("relocated/gate.py",
                        "def old_feature():\n" + pre + "    old_line()\n")
        base = self.repo.commit("base")
        self.repo.write("gate.py",
                        "def corrected():\n"
                        "    shared_before_1()\n"
                        "    shared_before_2()\n"
                        "    shared_before_3()\n"
                        "    shared_after_1()\n"
                        "    shared_after_2()\n"
                        "    shared_after_3()\n")
        self.repo.write("docs/review.md", "own review line\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.git("rm", "-q", "gate.py")
        self.repo.write("relocated/gate.py",
                        "def corrected():\n"
                        "    shared_before_1()\n"
                        "    shared_before_2()\n"
                        "    shared_before_3()\n"
                        "    shared_after_1()\n"
                        "    shared_after_2()\n"
                        "    shared_after_3()\n"
                        "def old_feature():\n" + pre + "    old_line()\n")
        head = self.repo.commit("impl: contenu corrigé regroupé", agent="impl")
        self.assertTrue(self.survives(head, fix, base))

    def test_a_reintroduced_pre_image_beside_a_duplicate_is_an_annulment(self):
        # B11 : la pré-image existe déjà dans une fonction indépendante, mais
        # la tête la réintroduit aussi au site corrigé (une occurrence de plus
        # que dans le fichier corrigé) : le retrait est bien annulé.
        pre = ("    shared_before_1()\n"
               "    shared_before_2()\n"
               "    shared_before_3()\n"
               "    return None\n"
               "    shared_after_1()\n"
               "    shared_after_2()\n"
               "    shared_after_3()\n")
        self.repo.write("gate.py", "def corrected():\n" + pre)
        self.repo.write("relocated/gate.py",
                        "def old_feature():\n" + pre + "    old_line()\n")
        base = self.repo.commit("base")
        self.repo.write("gate.py",
                        "def corrected():\n"
                        "    shared_before_1()\n"
                        "    shared_before_2()\n"
                        "    shared_before_3()\n"
                        "    shared_after_1()\n"
                        "    shared_after_2()\n"
                        "    shared_after_3()\n")
        self.repo.write("docs/review.md", "own review line\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.git("rm", "-q", "gate.py")
        self.repo.write("relocated/gate.py",
                        "def corrected():\n" + pre
                        + "def old_feature():\n" + pre + "    old_line()\n")
        head = self.repo.commit("impl: garde réintroduite", agent="impl")
        self.assertFalse(self.survives(head, fix, base))

    def test_a_reintroduced_removal_pre_image_is_an_annulment(self):
        # B9/B10 : l'annulation n'est prouvée que par la pré-image localisée
        # du hunk (contexte + lignes retirées) retrouvée d'un seul tenant. Ici
        # la tête réintroduit la garde exactement à sa place : le retrait est
        # bien annulé, malgré le `return None` d'`old_feature`.
        self.repo.write("gate.py",
                        "def corrected():\n"
                        "    return None\n"
                        "    guard_line_1()\n"
                        "    guard_line_2()\n"
                        "    guard_line_3()\n"
                        "    guard_line_4()\n")
        self.repo.write("relocated/gate.py",
                        "def old_feature():\n"
                        "    return None\n"
                        "    old_line()\n")
        base = self.repo.commit("base")
        self.repo.write("gate.py",
                        "def corrected():\n"
                        "    guard_line_1()\n"
                        "    guard_line_2()\n"
                        "    guard_line_3()\n"
                        "    guard_line_4()\n")
        self.repo.write("docs/review.md", "own review line\n")
        fix = self.repo.commit("fix", agent="cx3")
        self.repo.git("reset", "-q", "--hard", base)
        self.repo.git("rm", "-q", "gate.py")
        self.repo.write("relocated/gate.py",
                        "def corrected():\n"
                        "    return None\n"
                        "    guard_line_1()\n"
                        "    guard_line_2()\n"
                        "    guard_line_3()\n"
                        "    guard_line_4()\n"
                        "def old_feature():\n"
                        "    return None\n"
                        "    old_line()\n")
        head = self.repo.commit("impl: garde réintroduite", agent="impl")
        self.assertFalse(self.survives(head, fix, base))
