# SPDX-License-Identifier: AGPL-3.0-only
"""#8308: `release-check` and a wait for a review are not deployment waits.

The 26/11 20:26Z staging alert counted three lots because two hold notes
named merged lots without waiting for staging: the first was reading
`release-check` as a `release`, the second was reading a review wait as a
deployment wait. These are the two exact clauses, with the positive witnesses
from the same notes.
"""
import unittest

from ameesh.staging_pending import _deployment_refs


class ReviewWaitTest(unittest.TestCase):
    def test_release_check_is_not_a_release(self):
        # Exact clause of the 26/11 20:26Z alert on #8026.
        self.assertEqual(
            _deployment_refs(
                "release-check AG3 = 1 (#8290 et #8291 en relecture)"),
            (set(), set()))
        for note in ("release-check AG3 = 1 (#8290)",
                     "release check AG3 = 1 (#8290)",
                     "`ameesh host release-check ameesh-agents-3` (#8290)"):
            with self.subTest(note=note):
                self.assertEqual(_deployment_refs(note), (set(), set()))
        # A real release wait still counts.
        self.assertEqual(_deployment_refs("la release doit porter #8294"),
                         ({8294}, set()))

    def test_review_wait_is_excluded_over_the_whole_clause(self):
        # Exact clause of the same alert on #8035: the references come before
        # the review wait, far from it.
        self.assertEqual(
            _deployment_refs(
                "Les lots ouverts du jour (#8264, #8290, #8296) restent pour "
                "le moment tous en attente d’avis Codex"),
            (set(), set()))
        for note in ("attente de relecture pour #8264",
                     "attente du verdict codex sur #8296",
                     "avis codex du gel #46542 (4163d832) attendu à la reprise"):
            with self.subTest(note=note):
                self.assertEqual(_deployment_refs(note), (set(), set()))

    def test_deployment_wait_witnesses_are_kept(self):
        for note, expected in (
                ("attendre #8294 en staging", {8294}),
                ("attente de relecture pour #8264 ; le staging attend #8294",
                 {8294}),
                ("attendre que le correctif #8296 soit dans la release",
                 {8296})):
            with self.subTest(note=note):
                self.assertEqual(_deployment_refs(note),
                                 (expected, set()))

    def test_review_word_elsewhere_does_not_hide_a_deployment_wait(self):
        # #7647: the wait is the owner go; "avis ok auditeur acquis" is a
        # realised review in the same clause, not a wait for one.
        note = ("attente owner : go B6 (après #7634 livré et #7635 qualifié, "
                "tête gelée 3bfec59e560a, avis ok auditeur acquis)")
        self.assertEqual(_deployment_refs(note), ({7634, 7635}, set()))


if __name__ == "__main__":
    unittest.main()
