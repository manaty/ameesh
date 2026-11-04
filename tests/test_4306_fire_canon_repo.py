# SPDX-License-Identifier: AGPL-3.0-only
"""#4306 : le feu d'un lot du canon (`ameesh work fire`).

Un lot du canon porte une issue manaty/ameesh, mais sa branche et sa PR vivent
dans le dépôt du canon (home#210 à #212, 28/10). Le dépôt du lot se lisait par
son issue (`work.lot_repository`) : le passage cherchait la PR dans
manaty/ameesh et la garde du paquet (#2333) refusait. Depuis le clone d'un
canon approuvé, son `origin` l'emporte ; ailleurs, rien ne change.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from ameesh import fire

from .test_1003_merge_pr import _git

A = "a" * 40
ITEM = {"id": 4306, "title": "t", "state": "qa", "branch": "auditeur/4306-x",
        "branch_target": "main", "issue_ref": "manaty/ameesh#3283", "app": "ameesh",
        "assignee": ""}


def _clone(root: str, name: str, url: str) -> str:
    path = os.path.join(root, name)
    os.makedirs(path)
    _git(path, "init", "-q")
    _git(path, "remote", "add", "origin", url)
    return path


class CanonLotRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-4306-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = _clone(self.tmp, "home", "git@github.com:manaty/home.git")
        self.ameesh = _clone(self.tmp, "ameesh", "git@github.com:manaty/ameesh.git")
        self.cfg = types.SimpleNamespace(canon=self.home, canon_ref="",
                                         canon_untrusted=False)

    def test_the_canon_clone_wins_over_the_issue(self):
        self.assertEqual(fire._slug(self.cfg, ITEM, self.home), "manaty/home")

    def test_elsewhere_the_issue_still_names_the_repository(self):
        cases = {"product clone": (self.cfg, self.ameesh),
                 "no checkout": (self.cfg, None),
                 "untrusted canon": (types.SimpleNamespace(
                     canon=self.home, canon_ref="", canon_untrusted=True), self.home),
                 "no cfg": (None, self.home)}
        for label, (cfg, repo) in cases.items():
            with self.subTest(label):
                self.assertEqual(fire._slug(cfg, ITEM, repo), "manaty/ameesh")

    def _give(self, repo):
        marks = [{"kind": "frozen", "sha": A, "actor": "auditeur", "at_ts": 1.0, "id": 1}]
        with mock.patch.object(fire.work, "get", return_value=dict(ITEM)), \
                mock.patch.object(fire.work, "milestones", return_value=marks), \
                mock.patch.object(fire.merge_queue, "pinned", return_value=A), \
                mock.patch.object(fire.work, "milestone",
                                  side_effect=lambda db, lot, kind, **kw: dict(kw, kind=kind)):
            return fire._give_locked(None, 4306, sha=A, actor="mesh-design",
                                     repo=repo, cfg=self.cfg)

    def test_the_fire_is_given_from_the_canon_clone(self):
        row = self._give(self.home)
        self.assertEqual((row["kind"], row["sha"]), ("fire", A))

    def test_from_a_product_clone_the_packet_guard_refuses_and_names_the_gesture(self):
        with self.assertRaises(fire.FireError) as caught:
            self._give(self.ameesh)
        self.assertIn("§11 bis", str(caught.exception))
        self.assertIn("--repo <its clone>", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
