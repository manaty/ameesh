# SPDX-License-Identifier: AGPL-3.0-only
"""#6826 : rattrapage explicite dans le dépôt du code, distinct du ticket."""
import os
from unittest import mock

from ameesh import review_lot, work
from .test_1104_review_lot_hors_file import _Witness, _git


class DeliveredRepositoryTest(_Witness):
    def prepare(self):
        _git(self.repo, "remote", "add", "origin", "https://github.com/o/code.git")
        lot = work.add(self.db, title="code hors du dépôt du ticket", app="ameesh",
                       issue_ref="o/tickets#8", actor="orch")["id"]
        frozen = self.frozen_on_branch(lot, "lot/6826-code")
        # Première livraison sans clone disponible pour la relecture.
        with mock.patch.object(review_lot, "_candidate_repos", return_value=[]):
            first = work.merged(self.db, lot, sha=frozen, actor="claude1")
        self.assertEqual(first["result"], "merged", first)
        self.assertEqual(work.get(self.db, lot)["state"], "delivered")
        self.assertEqual(self.review_lots(lot), [])
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "merge code", "lot/6826-code")
        return lot, _git(self.repo, "rev-parse", "HEAD")

    def test_explicit_code_repository_retries_delivered_lot(self):
        lot, sha = self.prepare()
        before = work.milestones(self.db, lot)
        with mock.patch.object(review_lot, "_repo_of_agent", return_value=""), \
                mock.patch.object(review_lot.lot_class, "repository", return_value=None):
            result = work.merged(self.db, lot, sha=sha, repo=self.repo, actor="claude1")
            again = work.merged(self.db, lot, sha=sha, repo=self.repo, actor="claude1")
        self.assertEqual(result["result"], "already", result)
        self.assertTrue(result["review_lot"]["created"], result)
        self.assertEqual(len(self.review_lots(lot)), 1)
        self.assertEqual(again["review_lot"]["id"], result["review_lot"]["id"])
        self.assertEqual(work.get(self.db, lot)["state"], "delivered")
        self.assertEqual(work.milestones(self.db, lot), before)

    def test_invalid_explicit_repository_does_not_fall_back(self):
        lot, sha = self.prepare()
        # Le registre possède pourtant un clone lisible : --repo reste autoritaire.
        result = work.merged(self.db, lot, sha=sha,
                             repo=os.path.join(self.tmp, "absent"), actor="claude1")
        self.assertFalse(result["review_lot"]["created"], result)
        self.assertNotIn("dépôt inconnu", result["detail"])
        self.assertEqual(self.review_lots(lot), [])
        self.assertEqual(work.get(self.db, lot)["state"], "delivered")

    def test_first_delivery_still_requires_repository_proof(self):
        _git(self.repo, "remote", "add", "origin", "https://github.com/o/code.git")
        lot = work.add(self.db, title="première livraison", app="ameesh",
                       issue_ref="o/tickets#9", actor="orch")["id"]
        self.frozen_on_branch(lot, "lot/6826-first")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "merge", "lot/6826-first")
        sha = _git(self.repo, "rev-parse", "HEAD")
        self.proof_patcher.stop()
        self.addCleanup(self.proof_patcher.start)
        with self.assertRaises(work.WorkError):
            work.merged(self.db, lot, sha=sha, repo=self.repo, actor="claude1")
        self.assertNotEqual(work.get(self.db, lot)["state"], "delivered")
