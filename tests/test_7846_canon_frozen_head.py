# SPDX-License-Identifier: AGPL-3.0-only
"""#7846 — la tête gelée d'un lot du dépôt canon se lit sans attendre un fetch.

Un lot dont l'issue vit sur manaty/ameesh peut geler une tête du dépôt canon
(manaty/home). `_repository` ne cherchait et ne rapatriait la tête que dans
les clones du dépôt de l'issue : la désignation au gel puis la reprise après
synchronisation du canon répondaient « no repository holds the frozen head »
et écrivaient chacune au pilote, une minute avant que la passe suivante ne la
lise. Aucun témoin n'a besoin de la base.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import review_designation_activation as act


def _git(root: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    if proc.returncode:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class CanonFrozenHeadTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="t7846-")
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.origin = os.path.join(root, "home.git")
        os.makedirs(self.origin)
        _git(self.origin, "init", "-q", "--bare")
        seed = os.path.join(root, "seed")
        os.makedirs(seed)
        _git(seed, "init", "-q")
        _git(seed, "config", "user.name", "t")
        _git(seed, "config", "user.email", "t@example.invalid")
        with open(os.path.join(seed, "a.md"), "w", encoding="utf-8") as handle:
            handle.write("base\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-qm", "base")
        _git(seed, "push", "-q", self.origin, "HEAD:refs/heads/main")
        with open(os.path.join(seed, "a.md"), "w", encoding="utf-8") as handle:
            handle.write("frozen\n")
        _git(seed, "commit", "-qam", "frozen")
        self.frozen = _git(seed, "rev-parse", "HEAD")
        _git(seed, "push", "-q", self.origin, "HEAD:refs/heads/lot/7257-x")
        # Le clone du canon de l'hôte : main seul, sans la branche du lot.
        self.canon = os.path.join(root, "canon")
        subprocess.run(["git", "clone", "-q", "--branch", "main", "--single-branch",
                        "file://" + self.origin, self.canon], check=True, capture_output=True)
        _git(self.canon, "config", "remote.origin.url",
             "https://github.com/manaty/home.git")
        _git(self.canon, "config", "url.%s.insteadOf" % self.origin,
             "https://github.com/manaty/home.git")
        self.session = os.path.join(root, "session")
        os.makedirs(self.session)
        self.cfg = types.SimpleNamespace(host="h", radar={}, canon=self.canon)
        self.item = {"id": 7257, "app": "ameesh", "assignee": "author",
                     "branch": "lot/7257-x", "issue_ref": "manaty/ameesh#5475"}

    def _repository(self, *, marks=(), fetch=True, hint=""):
        rows = [{"kind": "frozen", "sha": self.frozen,
                 "note": "ameesh-freeze-repo: %s" % mark} for mark in marks]
        work = mock.Mock()
        work.milestones.return_value = rows
        with mock.patch.object(act.registry, "get",
                               return_value={"cwd": self.session}), \
                mock.patch.object(act, "_tracked_worktrees", return_value=[]), \
                mock.patch.object(act.storage, "of",
                                  return_value=types.SimpleNamespace(work=work)), \
                mock.patch.object(act.os, "getcwd", return_value=self.session):
            return act._repository(object(), self.cfg, self.item, self.frozen,
                                   fetch=fetch, hint=hint)

    def test_the_freeze_mark_fetches_the_head_into_the_canon_clone(self):
        self.assertEqual(self._repository(marks=["manaty/home"]),
                         os.path.realpath(self.canon))
        self.assertEqual(_git(self.canon, "cat-file", "-t", self.frozen), "commit")

    def test_without_the_mark_the_issue_repository_alone_is_fetched(self):
        # Sans marque, rien ne dit que la tête vit dans le canon : le clone du
        # canon (autre origine) n'est pas une cible de fetch.
        self.assertEqual(self._repository(), "")

    def test_the_canon_clone_holding_the_head_is_read_without_fetch(self):
        _git(self.canon, "fetch", "-q", "origin", self.frozen)
        self.assertEqual(self._repository(fetch=False),
                         os.path.realpath(self.canon))

    def test_a_neighbour_of_the_marked_repository_is_trusted(self):
        neighbour = os.path.join(self.tmp.name, "home-7257")
        subprocess.run(["git", "clone", "-q", self.origin, neighbour],
                       check=True, capture_output=True)
        _git(neighbour, "fetch", "-q", "origin", self.frozen)
        _git(neighbour, "config", "remote.origin.url",
             "https://github.com/manaty/home.git")
        self.cfg.canon = ""
        self.assertEqual(self._repository(fetch=False), "")
        self.assertEqual(self._repository(marks=["manaty/home"], fetch=False),
                         os.path.realpath(neighbour))

    def test_the_freeze_hint_is_read_first(self):
        hint = os.path.join(self.tmp.name, "worktree")
        subprocess.run(["git", "clone", "-q", self.origin, hint],
                       check=True, capture_output=True)
        _git(hint, "fetch", "-q", "origin", self.frozen)
        self.assertEqual(self._repository(fetch=False, hint=hint),
                         os.path.realpath(hint))


if __name__ == "__main__":
    unittest.main()
