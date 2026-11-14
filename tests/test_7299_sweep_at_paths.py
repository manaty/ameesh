# SPDX-License-Identifier: AGPL-3.0-only
"""#7299 — la preuve de contenu d'un gros commit lit ses chemins, pas l'arbre.

Cas réel #7266 (nexlink) : quatre commits d'un gel antérieur, dont deux gros
fichiers (une migration SQL et son témoin), faisaient lire l'arbre **entier**
du parent et celui de la base pour établir l'héritage (`git grep -G ^`). Sur un
arbre de 2,75 millions de lignes, l'entrée de cache dépassait
`MAX_ENTRY_BYTES`, n'était jamais publiée, et deux arbres ne tenaient pas dans
le mémo du processus : chaque commit repayait des dizaines de secondes, et le
balayage de 300 s échouait sur les trois hôtes (#7266, 5/5).

`_commit_survives` prouve maintenant d'abord le contenu au chemin du diff :
une ligne ajoutée, absente du parent **au même chemin**, et présente dans la
tête à ce chemin (ou à son chemin renommé) suffit. Aucun balayage d'arbre
entier n'est alors lancé. La conclusion « contenu disparu » garde la lecture
complète : le raccourci ne peut que conserver un auteur (canon §2).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from unittest import TestCase, mock

from ameesh import review_authors

from .test_2447_review_designation_activation import _Repo

#: au-delà de `TREE_LINES_WHOLE_MAX`, le balayage ciblé bascule sur l'arbre entier
BIG = ["valeur_%03d = fonction_%03d()" % (number, number)
       for number in range(420)]


class SurvivesAtPathTest(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-7299-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.write("base.py", "base = 1\n")
        self.base = self.repo.commit("base")

    def survives(self, head: str, sha: str):
        """`(résultat, étapes)` de `_commit_survives`, balayages observés."""
        steps: list = []
        original = review_authors._sweep_grep

        def spy(repo, rev, args, patterns, step):
            steps.append(step)
            return original(repo, rev, args, patterns, step)

        with mock.patch.object(review_authors, "_sweep_grep", side_effect=spy):
            value = review_authors._commit_survives(
                self.repo.root, head, sha, {}, self.base)
        return value, steps

    def test_a_surviving_large_commit_is_proven_at_its_path(self):
        self.repo.write("gros.py", "\n".join(BIG) + "\n")
        prior = self.repo.commit("gel antérieur : gros fichier")
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("gros.py", "\n".join(BIG) + "\n")
        head = self.repo.commit("réécriture : même contenu")

        value, steps = self.survives(head, prior)
        self.assertTrue(value)
        self.assertFalse([step for step in steps if "-G" in step],
                         "l'arbre entier est encore balayé : %s" % steps)

    def test_a_surviving_large_commit_is_proven_at_its_renamed_path(self):
        self.repo.write("ancien.py", "\n".join(BIG) + "\n")
        prior = self.repo.commit("gel antérieur : gros fichier")
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("nouveau.py", "\n".join(BIG) + "\n")
        head = self.repo.commit("réécriture : fichier renommé")
        # `git diff -M` doit voir le renommage, comme pour #7266 (795 → 796).
        self.assertEqual(
            self.repo.git("diff", "--name-status", "-M", prior, head).split("\t")[0],
            "R100")

        value, steps = self.survives(head, prior)
        self.assertTrue(value)
        self.assertFalse([step for step in steps if "-G" in step],
                         "l'arbre entier est encore balayé : %s" % steps)

    def test_a_rewritten_large_commit_is_still_not_carried(self):
        self.repo.write("gros.py", "\n".join(BIG) + "\n")
        prior = self.repo.commit("gel antérieur : gros fichier")
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("gros.py", "def remplace():\n    return 1\n")
        head = self.repo.commit("réécriture réelle")

        value, _steps = self.survives(head, prior)
        self.assertFalse(value)


if __name__ == "__main__":
    import unittest

    unittest.main()
