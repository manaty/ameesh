# SPDX-License-Identifier: AGPL-3.0-only
"""#5221 — le gel rafraîchit l'empreinte dans le dépôt qui a résolu sa tête.

Constat (mesh-design #177481) : le gel de #5203, #5204 et #5191 avertissait
« footprint not refreshed from the frozen head: … unreadable in … ». Le
rafraîchissement (#4173) relisait le diff dans le clone déclaré du projet
(ameesh-packets), qui n'a ni la branche ni le commit du lot, au lieu du dépôt
où `freeze_head` venait de résoudre la tête gelée.
"""
from __future__ import annotations

import os
import subprocess
from unittest import mock

from ameesh import intake_neighbors, storage, work

from .support import PgTestCase


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, env=env)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _commit(repo, name, text):
    path = os.path.join(repo, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", text)
    return _git(repo, "rev-parse", "HEAD")


class FreezeFootprintRepoTest(PgTestCase):
    def setUp(self):
        super().setUp()
        # le dépôt de l'auteur : `main` publiée, puis le commit du lot
        self.author = os.path.join(self.tmp, "auteur")
        os.makedirs(self.author)
        _git(self.author, "init", "-q", "-b", "main")
        base = _commit(self.author, "a.py", "base")
        _git(self.author, "update-ref", "refs/remotes/origin/main", base)
        _git(self.author, "checkout", "-q", "-b", "lot/x")
        self.head = _commit(self.author, "src/ameesh/b.py", "lot")
        # le clone déclaré : même `main`, sans la branche ni le commit du lot
        self.declared = os.path.join(self.tmp, "paquets")
        os.makedirs(self.declared)
        _git(self.declared, "init", "-q", "-b", "main")
        _commit(self.declared, "a.py", "base")
        self.register("auteur", "codex", cwd=self.author)
        self.lot = work.add(self.db, title="lot gelé", app="ameesh",
                            assignee="auteur", actor="auteur", branch="lot/x",
                            branch_target="main")["id"]
        work.move(self.db, self.lot, "build", actor="auteur")
        declared = mock.patch.object(intake_neighbors, "_declared_checkout",
                                     return_value=self.declared)
        declared.start()
        self.addCleanup(declared.stop)

    def _diff_paths(self):
        return sorted(r["path"] for r in
                      storage.of(self.db).work_footprints.for_items([self.lot])
                      if r["source"] == "diff")

    def test_repo_explicite_du_gel(self):
        work.milestone(self.db, self.lot, "frozen", sha=self.head,
                       actor="auteur", repo=self.author)
        self.assertEqual(self._diff_paths(), ["src/ameesh/b.py"])

    def test_depot_de_l_assigne_qui_a_resolu_la_tete(self):
        # sans `--repo` : `freeze_head` résout la tête dans le dossier de
        # l'assigné (registre) ; le diff se lit là, pas dans le clone déclaré
        work.milestone(self.db, self.lot, "frozen", sha=self.head, actor="auteur")
        self.assertEqual(self._diff_paths(), ["src/ameesh/b.py"])
