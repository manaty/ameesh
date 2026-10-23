# SPDX-License-Identifier: AGPL-3.0-only
"""#3068 — la classe d'un lot sans cible nommée se lit contre `origin/<cible>`.

Constat 23/10 : `ameesh work show 2955 --json` rendait `sensitive`, 60
fichiers, `base_sha: main`, alors que le gel 79be0a19 ne touchait que trois
fichiers légers depuis `origin/main`. Dans le clone partagé des agents, `main`
local était resté au 21/10 : `of_lot`, appelé sans `base` ni `base_sha` (vue
`work show`, stagnation), lisait le diff depuis cette branche locale et
rapportait au lot tout ce qui avait été fusionné depuis.

La file (#2207) et le feu (#2505) passaient déjà `origin/<cible>` ; la cible
d'atterrissage que `of_lot` résout lui-même suit désormais la même règle.
Témoin Git réel : upstream à A, agents clonés à A, upstream avance à B avec
une migration déjà fusionnée, le lot léger C part de B.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import lot_class, work

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
MIGRATION = "src/ameesh/migrations/9999_already_merged.sql"


def _git(root: str, *args: str) -> str:
    return subprocess.run(["git", "-C", root, *args], check=True, env=ENV,
                          capture_output=True, text=True).stdout.strip()


def _commit(root: str, path: str, text: str, message: str) -> str:
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full) or root, exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


class UnnamedTargetGitTest(unittest.TestCase):
    """#3068 : `of_lot` sans `base` ne lit jamais une branche locale périmée."""

    def canon(self):
        # Politique du projet : tout `.sql` est sensible, le reste léger.
        return types.SimpleNamespace(
            root="canon", sources=[],
            federation={"review_policies": {"risk_classes": {
                "projects": {"ameesh": {
                    "default": "light",
                    "rules": [{"paths": ["**/*.sql"], "class": "sensitive"}]}}}}})

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3068-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(work, "configured_canons",
                                    return_value=[self.canon()])
        patcher.start()
        self.addCleanup(patcher.stop)

    def witness(self):
        """(agents, A, B, C) : `main` local A, `origin/main` B, lot léger C."""
        upstream = os.path.join(self.tmp, "upstream")
        os.makedirs(upstream)
        _git(upstream, "init", "-q", "-b", "main")
        a = _commit(upstream, "README.md", "A\n", "A")
        agents = os.path.join(self.tmp, "agents")
        _git(self.tmp, "clone", "-q", "--branch", "main", upstream, agents)
        b = _commit(upstream, MIGRATION, "-- merged\n", "B, already merged")
        _git(upstream, "checkout", "-q", "-b", "lot/light", b)
        c = _commit(upstream, "README.md", "C\n", "C, light")
        _git(upstream, "checkout", "-q", "main")
        # le clone partagé récupère l'origin sans jamais avancer son `main`
        _git(agents, "fetch", "-q", "origin")
        self.assertEqual(_git(agents, "rev-parse", "main"), a)
        self.assertEqual(_git(agents, "rev-parse", "origin/main"), b)
        return agents, a, b, c

    @staticmethod
    def row(**extra):
        return dict({"id": 42, "title": "lot léger", "branch": "lot/light",
                     "assignee": "author", "app": "ameesh"}, **extra)

    @staticmethod
    def marks(sha):
        return [{"id": 1, "kind": "frozen", "sha": sha, "at_ts": 1.0}]

    def test_a_declared_target_is_read_on_origin(self):
        # la forme de `work show` : ni `base`, ni `base_sha`, ni `head`
        agents, _a, _b, c = self.witness()
        klass = lot_class.of_lot(None, 42, cfg=None, item=self.row(branch_target="main"),
                                 repo=agents, milestones=self.marks(c))
        self.assertEqual(klass["class"], "light", klass["why"])
        self.assertEqual([entry["path"] for entry in klass["files"]], ["README.md"])
        self.assertEqual(klass["target"], "main")
        self.assertEqual(klass["base_sha"], "origin/main")

    def test_the_clone_default_target_is_read_on_origin(self):
        # sans `branch_target` ni projet : `origin/HEAD` du clone, déjà distante
        agents, _a, _b, c = self.witness()
        klass = lot_class.of_lot(None, 42, cfg=None, item=self.row(),
                                 repo=agents, milestones=self.marks(c))
        self.assertEqual(klass["class"], "light", klass["why"])
        self.assertEqual(klass["base_sha"], "origin/main")

    def test_a_base_named_by_the_caller_is_kept(self):
        # l'appelant qui nomme sa base en reste juge (#2464) : rien n'est
        # réécrit, même une branche locale périmée
        agents, _a, _b, c = self.witness()
        klass = lot_class.of_lot(None, 42, cfg=None, item=self.row(), repo=agents,
                                 base="main", head=c)
        self.assertEqual(klass["class"], "sensitive", klass["why"])
        self.assertEqual(klass["base_sha"], "main")
        self.assertIn(MIGRATION, [entry["path"] for entry in klass["files"]])

    def test_without_a_remote_the_local_target_is_read(self):
        # un dépôt sans distant (clone local, banc) garde sa cible locale
        local = os.path.join(self.tmp, "local")
        os.makedirs(local)
        _git(local, "init", "-q", "-b", "main")
        _commit(local, "README.md", "A\n", "A")
        _git(local, "checkout", "-q", "-b", "lot/light")
        c = _commit(local, "README.md", "C\n", "C, light")
        klass = lot_class.of_lot(None, 42, cfg=None, item=self.row(branch_target="main"),
                                 repo=local, milestones=self.marks(c))
        self.assertEqual(klass["class"], "light", klass["why"])
        self.assertEqual(klass["base_sha"], "main")


if __name__ == "__main__":
    unittest.main()
