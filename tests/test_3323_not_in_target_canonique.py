# SPDX-License-Identifier: AGPL-3.0-only
"""#3323 — un SHA non canonique passé à `_not_in_target` ne retire pas l'auteur.

`_not_in_target` (#3116) écarte de la provenance les commits déjà atteignables
depuis la cible. La sortie de `git rev-list` est toujours canonique (complète,
en minuscules) ; un appelant peut pourtant passer une entrée abrégée ou en
majuscules. Comparer l'entrée telle quelle la faisait passer pour « dans la
cible » alors qu'elle ne l'est pas : un auteur disparaissait de la lecture.

Attendu (invariant « le doute ne retire jamais un auteur ») : on ne retire que
l'entrée **prouvée** dans la cible, et l'entrée est résolue avant comparaison
(`rev-parse`) ; une entrée illisible reste. Le filtrage des commits réellement
dans la cible est inchangé, y compris sous forme abrégée ou en majuscules.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from ameesh import review_authors

from .test_2447_review_designation_activation import _Repo


class NotInTargetCanonicalTest(unittest.TestCase):
    """Un dépôt minimal : une cible et un commit de lot hors cible."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3323-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.write("base.txt", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("target.txt", "merged in the target\n")
        self.target = self.repo.commit("merged target lot")
        self.repo.git("checkout", "-q", "-b", "lot-3323", self.base)
        self.repo.write("lot.txt", "the lot commit\n")
        self.outside = self.repo.commit("lot commit outside the target")

    def not_in_target(self, commits):
        return review_authors._not_in_target(self.repo.root, commits, self.target)

    def test_an_abbreviated_outside_commit_is_kept(self):
        self.assertEqual(self.not_in_target([self.outside[:12]]), [self.outside])

    def test_an_uppercase_outside_commit_is_kept(self):
        self.assertEqual(self.not_in_target([self.outside.upper()]), [self.outside])

    def test_an_abbreviated_target_commit_is_still_removed(self):
        self.assertEqual(self.not_in_target([self.target[:12]]), [])

    def test_an_uppercase_target_commit_is_still_removed(self):
        self.assertEqual(self.not_in_target([self.target.upper()]), [])

    def test_a_full_canonical_list_is_unchanged(self):
        self.assertEqual(self.not_in_target([self.outside, self.target]),
                         [self.outside])

    def test_a_mixed_list_keeps_only_the_proven_outside_commit(self):
        self.assertEqual(
            self.not_in_target([self.target, self.outside[:12], self.outside.upper()]),
            [self.outside, self.outside])

    def test_an_unresolvable_entry_is_kept(self):
        # doute : l'entrée illisible n'est pas « prouvée dans la cible ».
        self.assertEqual(self.not_in_target(["deadbeef"]), ["deadbeef"])


if __name__ == "__main__":
    unittest.main()
