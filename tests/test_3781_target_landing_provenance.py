# SPDX-License-Identifier: AGPL-3.0-only
"""#3781: historical integration requires a positive current landing proof."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import review_authors as authors
from .test_2447_review_designation_activation import _Repo


class LandingProofTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3781-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = _Repo(os.path.join(self.tmp, "repo"))
        self.repo.write("code.py", "old_implementation()\n")
        self.base = self.repo.commit("base")
        self.repo.write("code.py", "reviewed_implementation()\n")
        self.original = self.repo.commit("reviewed fix", agent="cx")
        self.repo.git("checkout", "-qb", "target", self.base)
        self.repo.write("anchor", "target advanced\n")
        self.repo.commit("anchor")
        self.repo.git("cherry-pick", self.original)
        self.carrier = self.repo.git("rev-parse", "HEAD")
        self.repo.write("code.py", "evolved_implementation()\n")
        self.target = self.repo.commit("legitimate evolution")
        # The branch was updated after integration, before later target edits.
        self.repo.git("checkout", "-qb", "lot", self.carrier)
        self.repo.write("own.py", "independent_contribution()\n")
        self.head = self.repo.commit("own change")

    def remaining(self, head=None):
        return authors._not_in_target(self.repo.root, [self.original], self.target,
                                      head=self.head if head is None else head)

    def test_evolution_is_excluded_with_positive_landing_proof(self):
        self.assertFalse(authors._applied_in(self.repo.root, self.target, self.original))
        self.assertEqual(self.remaining(), [])

    def test_missing_head_keeps_author(self):
        self.assertEqual(self.remaining(""), [self.original])

    def test_unreadable_head_keeps_author(self):
        self.assertEqual(self.remaining("missing-object"), [self.original])

    def test_rewrite_reintroduced_by_head_keeps_author(self):
        self.repo.git("checkout", "-qb", "rewritten", self.target)
        self.repo.write("code.py", "reviewed_implementation()\n")
        self.head = self.repo.commit("bring back historical content")
        self.assertEqual(self.remaining(), [self.original])

    def test_revert_reintroduced_by_head_keeps_author(self):
        self.repo.git("checkout", "-q", "target")
        self.repo.git("reset", "--hard", self.carrier)
        self.repo.git("revert", "--no-edit", self.carrier)
        self.target = self.repo.git("rev-parse", "HEAD")
        self.repo.write("code.py", "reviewed_implementation()\n")
        self.head = self.repo.commit("reapply on branch")
        self.assertEqual(self.remaining(), [self.original])

    def test_reapplication_on_target_is_excluded(self):
        self.repo.git("checkout", "-q", "target")
        self.repo.write("code.py", "reviewed_implementation()\n")
        self.target = self.repo.commit("reapply on target")
        self.assertEqual(self.remaining(), [])

    def test_conflicting_whole_head_keeps_author(self):
        self.repo.write("code.py", "conflicting_branch_change()\n")
        self.head = self.repo.commit("conflict")
        self.assertEqual(self.remaining(), [self.original])

    def test_removed_line_reintroduced_as_deletion_keeps_author(self):
        self.repo.git("checkout", "-qb", "deletion", self.target)
        self.repo.write("code.py", "old_implementation()\n")
        self.target = self.repo.commit("restore removed line")
        self.repo.write("code.py", "different_branch_implementation()\n")
        self.head = self.repo.commit("remove old line again")
        self.assertEqual(self.remaining(), [self.original])

    def test_moved_contribution_keeps_author(self):
        self.repo.git("checkout", "-qb", "move", self.target)
        self.repo.write("elsewhere.py", "reviewed_implementation()\n")
        self.head = self.repo.commit("move historical contribution")
        self.assertEqual(self.remaining(), [self.original])

    def test_unreadable_delta_keeps_author(self):
        with mock.patch.object(authors, "_text_delta", side_effect=OSError):
            self.assertEqual(self.remaining(), [self.original])

    def test_unreadable_candidate_delta_keeps_author(self):
        read = authors._text_delta
        def fail(repo, before, after):
            if after == self.original:
                raise OSError("candidate unreadable")
            return read(repo, before, after)
        with mock.patch.object(authors, "_text_delta", side_effect=fail):
            self.assertEqual(self.remaining(), [self.original])

    def test_binary_landing_keeps_author(self):
        with open(os.path.join(self.repo.root, "binary"), "wb") as stream:
            stream.write(b"a\0b")
        self.head = self.repo.commit("binary")
        self.assertEqual(self.remaining(), [self.original])

    def test_mode_change_keeps_author(self):
        os.chmod(os.path.join(self.repo.root, "code.py"), 0o755)
        self.head = self.repo.commit("executable")
        self.assertEqual(self.remaining(), [self.original])


class RealLandingWitnessTest(unittest.TestCase):
    """Frozen H8 witnesses; portable shallow checkouts may lack these objects."""
    def test_h8_against_frozen_target(self):
        from pathlib import Path
        from ameesh import plan_git
        repo = str(Path(__file__).resolve().parents[1])
        target = "38736fdad914fd5cb7b7dbbdad153776b7660263"
        head = "ee50e13b1f08b0dbcdc0c8bb7f6cb010d0914f54"
        copies = ["c051daf62e265fef8c302f01e2cf4da4b46cdcf5",
                  "ced6320dfa181f75eb68caee306641f14b7ea037",
                  "3f0f641bec21ab308e2b70cd5966f5e8ccc0e20a"]
        if any(not plan_git.local_commit(repo, sha)
               for sha in [target, head, *copies]):
            self.skipTest("historical H8 objects unavailable")
        self.assertEqual(authors._not_in_target(repo, copies, target), copies)
        # #3902 : les trois copies sont déjà dans la cible par contenu, et la
        # tête ne réintroduit aucune de leurs lignes **propres** (les lignes
        # génériques qui se recoupent sont héritées de leur parent). Le doute
        # de #3781 reste pour une contribution propre réintroduite (classes
        # ci-dessus), pas pour du contexte hérité.
        self.assertEqual(authors._not_in_target(repo, copies, target, head=head),
                         [])


if __name__ == "__main__":
    unittest.main()
