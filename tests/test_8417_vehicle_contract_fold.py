# SPDX-License-Identifier: AGPL-3.0-only
"""#8417 — le commit de pliage du contrat n'est pas un delta propre du véhicule.

Décision du pilote sur le N4 de la relecture de #8395 : le commit simple par
lequel le composeur plie les fragments du contrat module v1 est exempté quand
il est cité au manifeste du paquet, que le contrôle de pliage de
`paquet-inclusion` passe sur lui et qu'il ne touche aucun autre chemin. Sinon
il reste un delta propre : pas de repli silencieux. Les témoins : un pliage
vérifié est exempté ; un pliage qui touche un autre chemin compte ; un commit
non cité au manifeste compte ; un pliage dont le contenu diffère compte.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import merge_queue, packet_reviews, packet_snapshot

from .test_2766_vehicle_reviews import _commit_all, _merge, _write
from .test_l160b_radar import _git

CONTRACT = "docs/api/module-contract-v1.md"
FRAGMENT = "docs/api/module-contract-v1.d/500.md"
BRANCH = "integration/paquet-t"
MANIFEST = "reviews/integration-paquet-t.md"
BEFORE = ("Contract revision: **1.11, 1 January 2026**.\n\n"
          "Revision 1.11 adds eleven (#11); it is additive to 1.10.\n")
FOLDED = ("Contract revision: **1.12, 2 January 2026**.\n\n"
          "Revision 1.11 adds eleven (#11); it is additive to 1.10.\n"
          "Revision 1.12 adds a read; it is additive to 1.11.\n\n"
          "## Demo\n\nThe body.\n")


class ContractFoldVehicleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-8417-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # le manifeste se lit dans l'arbre de la tête, pas dans un reçu
        patcher = mock.patch.object(packet_snapshot, "lookup", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _write(self.repo, CONTRACT, BEFORE)
        _write(self.repo, "src/app.py", "print(1)\n")
        self.base = _commit_all(self.repo, "base")
        _git(self.repo, "checkout", "-q", "-b", "lot/500", "main")
        _write(self.repo, FRAGMENT,
               "adds: a read; it is additive to {previous}.\n\n"
               "## Demo\n\nThe body.\n")
        self.member = _commit_all(self.repo, "#500 : contrat")
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "checkout", "-q", "-b", BRANCH)
        _merge(self.repo, "lot/500", 500)

    def fold(self, contract=FOLDED, extra=None):
        """Le commit de pliage : en-tête réécrit, fragment retiré."""
        _write(self.repo, CONTRACT, contract)
        os.unlink(os.path.join(self.repo, FRAGMENT))
        for path, text in (extra or {}).items():
            _write(self.repo, path, text)
        return _commit_all(self.repo, "contrat module v1 : révisions 1.12 (#500)")

    def manifest(self, cited):
        _write(self.repo, MANIFEST,
               "# Paquet\n\n## Contrat module v1\n\n"
               "- Révisions attribuées : 1.12 (#500, fragment).\n"
               "- Commit de composition : `%s` (en-tête, surface et "
               "`CONTRACT_VERSION` alignés ; fragments retirés).\n" % cited)
        return _commit_all(self.repo, "paquet t : manifeste")

    def own(self, head):
        fold = packet_reviews.contract_fold(None, 900, {"branch": BRANCH},
                                            self.repo, head)
        return fold, packet_reviews.own_changes(self.repo, self.base, head,
                                                [self.member], fold=fold)

    def test_a_verified_fold_is_not_a_proper_delta(self):
        fold = self.fold()
        head = self.manifest(fold)
        found, own = self.own(head)
        self.assertEqual(found, fold)
        self.assertEqual(own, {"paths": [], "commits": []})
        # sans la dispense, le même commit est un delta propre (N4 de #8395)
        bare = packet_reviews.own_changes(self.repo, self.base, head,
                                          [self.member])
        self.assertEqual(bare["paths"], [FRAGMENT, CONTRACT])
        self.assertEqual(bare["commits"], [fold])

    def test_a_cited_short_sha_is_resolved(self):
        fold = self.fold()
        head = self.manifest(fold[:12])
        found, own = self.own(head)
        self.assertEqual(found, fold)
        self.assertEqual(own["paths"], [])

    def test_a_fold_touching_another_path_counts(self):
        fold = self.fold(extra={"src/app.py": "print(2)\n"})
        head = self.manifest(fold)
        found, own = self.own(head)
        self.assertIsNone(found)
        self.assertEqual(own["paths"], [FRAGMENT, CONTRACT, "src/app.py"])
        self.assertEqual(own["commits"], [fold])

    def test_a_commit_not_cited_in_the_manifest_counts(self):
        cited = self.fold()
        _write(self.repo, CONTRACT, FOLDED + "\nA later edit.\n")
        later = _commit_all(self.repo, "adaptation du contrat")
        head = self.manifest(cited)
        found, own = self.own(head)
        self.assertEqual(found, cited)
        self.assertEqual(own["paths"], [CONTRACT])
        self.assertEqual(own["commits"], [later])

    def test_a_vehicle_without_a_cited_fold_counts(self):
        fold = self.fold()
        _write(self.repo, MANIFEST, "# Paquet\n\n## Contrat module v1\n\n"
               "- Aucun fragment de révision de contrat dans ce paquet.\n")
        head = _commit_all(self.repo, "paquet t : manifeste")
        found, own = self.own(head)
        self.assertIsNone(found)
        self.assertEqual(own["commits"], [fold])

    def test_a_fold_whose_content_differs_counts(self):
        fold = self.fold(contract=FOLDED.replace("adds a read", "adds a write"))
        head = self.manifest(fold)
        found, own = self.own(head)
        self.assertIsNone(found)
        self.assertEqual(own["paths"], [FRAGMENT, CONTRACT])
        self.assertEqual(own["commits"], [fold])

    def test_an_unreadable_manifest_grants_nothing(self):
        fold = self.fold()
        head = self.manifest(fold)
        with mock.patch.object(packet_snapshot, "lookup",
                               side_effect=packet_snapshot.SnapshotError("down")):
            found, own = self.own(head)
        self.assertIsNone(found)
        self.assertEqual(own["commits"], [fold])

    def test_a_non_vehicle_branch_has_no_fold(self):
        fold = self.fold()
        head = self.manifest(fold)
        self.assertIsNone(packet_reviews.contract_fold(
            None, 900, {"branch": "lot/500"}, self.repo, head))


class FoldCheckClassTest(unittest.TestCase):
    def test_the_shared_fold_check_stays_critical(self):
        """Le contrôle sort de `scripts/paquet-inclusion.py` (deux familles) :
        son module `src` garde la même classe."""
        for path in ("scripts/paquet-inclusion.py",
                     "src/ameesh/review_contract_fold.py"):
            with self.subTest(path=path):
                self.assertTrue(merge_queue._entry_critical(
                    {"path": path, "class": "light"}))


if __name__ == "__main__":
    unittest.main()
