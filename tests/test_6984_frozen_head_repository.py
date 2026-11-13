# SPDX-License-Identifier: AGPL-3.0-only
"""#6984 — lire une tête gelée dans un miroir bare et sur un hôte sans radar.

Le gel désigne et la porte lit les auteurs dans un dépôt **local** qui tient le
commit exact. Deux formes de dépôt échappaient à ces lectures :

* un miroir **bare** n'a pas d'arbre de travail ; `git rev-parse
  --show-toplevel` y échoue, donc `_repository` et `repo_for_freeze` ne le
  voyaient jamais, même après un fetch de la branche dans le miroir ;
* un hôte sans bloc `radar` (pc-smichea) ne déclare ses clones que par
  `packet_watch` ou `release` ; la désignation ne les essayait pas.

Les témoins ci-dessous fixent les deux lectures, plus le brief de session qui
répondait « diff unreadable: git failed » quand le dossier de session n'était
pas le worktree du lot. Aucun n'a besoin de la base.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import canon
from ameesh import lot_class
from ameesh import reprise
from ameesh import review_designation_activation as act


class _Repo:
    """Un dépôt git minimal, pour les témoins de lecture par SHA."""

    def __init__(self, root: str, *, bare: bool = False):
        self.root = root
        os.makedirs(root, exist_ok=True)
        args = ["init", "-q"]
        if bare:
            args.append("--bare")
        self.git(*args)
        if not bare:
            self.git("config", "user.name", "t")
            self.git("config", "user.email", "t@example.invalid")

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", self.root, *args],
                              capture_output=True, text=True)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, path: str, text: str) -> None:
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")


class FrozenHeadRepositoryTest(unittest.TestCase):
    """Un miroir bare est un dépôt lisible, pas un dossier sans git."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="t6984-")
        self.addCleanup(self.tmp.cleanup)
        self.seed = _Repo(os.path.join(self.tmp.name, "seed"))
        self.seed.write("a.txt", "base\n")
        self.base = self.seed.commit("base")
        self.seed.write("a.txt", "frozen\n")
        self.frozen = self.seed.commit("frozen")
        self.origin = _Repo(os.path.join(self.tmp.name, "origin.git"), bare=True)
        self.seed.git("remote", "add", "origin", self.origin.root)
        self.seed.git("push", "-q", "origin", "HEAD:refs/heads/main")
        self.seed.git("push", "-q", "origin", "HEAD:refs/heads/lot/6984")

    def _mirror(self, name="mirror.git") -> _Repo:
        mirror = _Repo(os.path.join(self.tmp.name, name), bare=True)
        mirror.git("remote", "add", "origin", self.origin.root)
        # Le miroir se déclare sur la forge du lot, mais fetch vers l'origine
        # locale : `_origin_matches` lit la configuration, pas l'URL réécrite.
        mirror.git("config", "remote.origin.url",
                   "https://github.com/manaty/ameesh.git")
        mirror.git("config", "url.%s.insteadOf" % self.origin.root,
                   "https://github.com/manaty/ameesh.git")
        mirror.git("fetch", "-q", "origin", "+refs/heads/*:refs/heads/*")
        return mirror

    def test_git_repository_reads_a_bare_mirror(self):
        mirror = self._mirror()
        self.assertEqual(canon.git_repository(mirror.root),
                         os.path.realpath(mirror.root))
        # un arbre de travail garde sa racine, un dossier hors dépôt reste None
        self.assertEqual(canon.git_repository(self.seed.root),
                         os.path.realpath(self.seed.root))
        self.assertIsNone(canon.git_repository(self.tmp.name))

    def test_a_bare_mirror_holds_the_frozen_head(self):
        mirror = self._mirror()
        self.assertEqual(act._repo_holding(mirror.root, self.frozen, set()),
                         os.path.realpath(mirror.root))

    def test_the_frozen_head_is_fetched_into_a_bare_mirror(self):
        # Un miroir qui ne tient pas encore la tête : le fetch borné de la
        # désignation doit pouvoir la lui apporter, puis la relire.
        mirror = self._mirror()
        mirror.git("update-ref", "-d", "refs/heads/lot/6984")
        mirror.git("reflog", "expire", "--expire=now", "--all")
        found = act._fetch_from_local_clone(
            [mirror.root], "manaty/ameesh", self.frozen, "lot/6984", set())
        self.assertEqual(found, os.path.realpath(mirror.root))
        self.assertEqual(mirror.git("cat-file", "-t", self.frozen), "commit")

    def test_the_project_bare_mirror_is_a_designation_candidate(self):
        mirror = self._mirror()
        session = os.path.join(self.tmp.name, "session")
        os.makedirs(session)
        cfg = types.SimpleNamespace(
            host="pc-smichea",
            radar={"projects": {"ameesh": {"repo": mirror.root}}})
        item = {"id": 6984, "app": "ameesh", "assignee": "author",
                "issue_ref": "manaty/ameesh#5294"}
        with mock.patch.object(act.registry, "get",
                               return_value={"cwd": session}), \
                mock.patch.object(act, "_tracked_worktrees", return_value=[]), \
                mock.patch.object(act.work, "lot_repository",
                                  return_value="manaty/ameesh"), \
                mock.patch.object(act.os, "getcwd", return_value=session):
            found = act._repository(None, cfg, item, self.frozen)
        self.assertEqual(found, os.path.realpath(mirror.root))

    def test_declared_repo_paths_include_packet_watch_and_release(self):
        cfg = types.SimpleNamespace(
            host="pc-smichea", radar={},
            packet_watch={"projects": {"ameesh": {"repo": "/srv/ameesh"}}},
            release={"projects": {"ameesh": {"repo": "/srv/ameesh-release"},
                                   "nexlink": {"repo": "/srv/nexlink"}}})
        self.assertEqual(lot_class.declared_repo_paths(cfg, {"app": "ameesh"}),
                         ["/srv/ameesh", "/srv/ameesh-release"])
        self.assertEqual(lot_class.declared_repo_paths(cfg, {"app": "nexlink"}),
                         ["/srv/nexlink"])
        self.assertEqual(lot_class.declared_repo_paths(cfg, {"app": ""}), [])

    def test_repo_for_freeze_reads_a_bare_mirror(self):
        mirror = self._mirror()
        session = os.path.join(self.tmp.name, "session-2")
        os.makedirs(session)
        cfg = types.SimpleNamespace(
            host="pc-smichea",
            radar={},
            packet_watch={"projects": {"ameesh": {"repo": mirror.root}}},
            release={})
        item = {"id": 6984, "app": "ameesh", "assignee": "author"}
        with mock.patch("ameesh.db.is_dry_pass", return_value=False), \
                mock.patch("ameesh.registry.get",
                           return_value={"cwd": session}), \
                mock.patch("ameesh.registry.overview", return_value=[]):
            found, commit = lot_class.repo_for_freeze(
                None, cfg, item, self.frozen)
        self.assertEqual(found, os.path.realpath(mirror.root))
        self.assertEqual(commit, self.frozen)


class RepriseLotDiffTest(unittest.TestCase):
    """Le brief lit la branche du lot dans son worktree suivi (#6984)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="t6984-diff-")
        self.addCleanup(self.tmp.cleanup)
        self.origin = _Repo(os.path.join(self.tmp.name, "origin.git"), bare=True)
        self.seed = _Repo(os.path.join(self.tmp.name, "seed"))
        self.seed.write("a.txt", "base\n")
        base = self.seed.commit("base")
        self.seed.git("remote", "add", "origin", self.origin.root)
        self.seed.git("push", "-q", "origin", "HEAD:refs/heads/main")
        self.seed.git("checkout", "-q", "-B", "lot/6984-brief", base)
        self.seed.write("b.txt", "lot\n")
        self.lot_head = self.seed.commit("lot")
        self.seed.git("push", "-q", "origin", "HEAD:refs/heads/lot/6984-brief")
        self.worktree = os.path.join(self.tmp.name, "worktree")
        subprocess.run(["git", "clone", "-q", self.origin.root, self.worktree],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", self.worktree, "checkout", "-q",
                        "lot/6984-brief"], check=True, capture_output=True)
        self.session = _Repo(os.path.join(self.tmp.name, "session"))
        self.session.write("session.txt", "session\n")
        self.session.commit("session")
        self.item = {"id": 6984, "branch": "lot/6984-brief",
                     "branch_target": "origin/main"}
        self.cfg = types.SimpleNamespace(host="ameesh-agents-1")

    def test_the_branch_is_read_in_the_tracked_worktree(self):
        class Db:
            def query(self, sql, params):
                self.sql = sql
                self.params = params
                return [{"path": self.worktree_path}]

        db = Db()
        db.worktree_path = self.worktree

        lines = reprise._lot_diff(self.item, self.session.root, db=db,
                                  cfg=self.cfg)
        text = "\n".join(lines)
        self.assertIn("lot/6984-brief", text)
        self.assertIn("read in %s" % os.path.realpath(self.worktree), text)
        self.assertIn("b.txt", text)

    def test_an_absent_branch_names_the_real_git_error(self):
        class Db:
            def query(self, sql, params):
                return []

        item = dict(self.item, branch="lot/6984-absente")
        text = "\n".join(reprise._lot_diff(item, self.session.root, db=Db(),
                                            cfg=self.cfg))
        self.assertIn("diff unreadable", text)
        self.assertNotIn("git failed", text)
        self.assertIn("Needed a single revision", text)


if __name__ == "__main__":
    unittest.main()
