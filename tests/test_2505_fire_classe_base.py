# SPDX-License-Identifier: AGPL-3.0-only
"""#2505 — la classe du feu se lit contre `origin/<cible>`, pas le local.

Constat 18/10 (#2497) : `run_pass(only_lot=2497)` rendait « sensitive, 2 avis »
sur un lot de deux fichiers de test. `fire.ready_verdicts` passait `base=target`
(« main ») à `lot_class.of_lot` ; dans le checkout de l'assigné, `main` avait
29 h de retard. Le diff de classe y ajoutait 407 fichiers déjà fusionnés, dont
`.github/**`, et surclassait le lot sensible.

Le feu lit désormais sa borne comme la file (#2207,
`merge_queue._class_base`) : `origin/<cible>` d'abord, la cible locale du
checkout seulement à défaut. Témoin Git réel : upstream à A, agents clonés à A,
puis upstream avance à B avec une migration déjà fusionnée et le lot léger part
de B. `main` local reste en A ; `origin/main` est B. Avec la cible locale le diff
A..C est sensible, avec `origin/main` B..C il est léger : le feu doit être prêt.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import fire, lot_class, merge_queue, work

FROZEN = "c" * 40
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


def _git(root: str, *args: str) -> str:
    return subprocess.run(["git", "-C", root, *args], check=True, env=ENV,
                          capture_output=True, text=True).stdout.strip()


def _write(root: str, path: str, text: str) -> None:
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full) or root, exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)


def _commit(root: str, path: str, text: str, message: str) -> str:
    _write(root, path, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


class StaleLocalTargetGitTest(unittest.TestCase):
    """#2505 : un `main` local en retard ne surclasse pas le lot au feu."""

    def canon(self):
        # Politique du projet : tout `.sql` est sensible, le reste léger.
        return types.SimpleNamespace(
            root="canon", sources=[],
            federation={"review_policies": {"risk_classes": {
                "projects": {"ameesh": {
                    "default": "light",
                    "rules": [{"paths": ["**/*.sql"], "class": "sensitive"}]}}}}})

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2505-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(work, "configured_canons",
                                    return_value=[self.canon()])
        patcher.start()
        self.addCleanup(patcher.stop)

    def witness(self):
        """(agents, A, B, C) : cible locale A, origin/main B, lot léger C."""
        upstream = os.path.join(self.tmp, "upstream")
        os.makedirs(upstream)
        _git(upstream, "init", "-q", "-b", "main")
        a = _commit(upstream, "README.md", "A\n", "A")
        agents = os.path.join(self.tmp, "agents")
        _git(self.tmp, "clone", "-q", "--branch", "main", upstream, agents)
        # B : un chemin sensible déjà fusionné dans la cible, hors du lot.
        b = _commit(upstream, "src/ameesh/migrations/9999_already_merged.sql",
                    "-- merged\n", "B, already merged")
        # C : le lot, qui ne touche que README.md depuis B.
        _git(upstream, "checkout", "-q", "-b", "lot/light", b)
        c = _commit(upstream, "README.md", "C\n", "C, light")
        _git(upstream, "checkout", "-q", "main")
        # les agents n'ont pas avancé leur `main` local ; l'origin est récupérée.
        _git(agents, "fetch", "-q", "origin")
        self.assertEqual(_git(agents, "rev-parse", "main"), a)
        self.assertEqual(_git(agents, "rev-parse", "origin/main"), b)
        return agents, a, b, c

    def test_a_stale_local_main_does_not_overclassify_the_fire(self):
        agents, _a, _b, c = self.witness()
        row = {"id": 42, "title": "lot léger", "branch": "lot/light",
               "assignee": "author", "app": "ameesh", "branch_target": "main"}
        marks = [{"id": 1, "kind": "frozen", "sha": c, "at_ts": 1.0}]
        # La cible locale périmée explique une classe sensible : la migration
        # de B n'appartient pas au lot (#2505, constat #2497).
        stale = lot_class.of_lot(None, 42, cfg=None, item=row, repo=agents,
                                 base="main", head=c)
        self.assertEqual(stale["class"], "sensitive", stale["why"])
        self.assertIn("src/ameesh/migrations/9999_already_merged.sql",
                      [entry["path"] for entry in stale["files"]])
        # Le feu lit origin/main : seul README.md reste dans le diff, léger,
        # et aucun avis n'est requis.
        ok, why, klass = fire.ready_verdicts(None, None, row, marks, repo=agents,
                                             project=None, target="main")
        self.assertTrue(ok, why)
        self.assertEqual(klass["class"], "light")
        self.assertEqual(klass["target"], "origin/main")
        self.assertEqual([entry["path"] for entry in klass["files"]], ["README.md"])


class ClassBaseResolutionTest(unittest.TestCase):
    """La borne passée à la classe : `origin/<cible>` d'abord, locale à défaut."""

    ROW = {"id": 42, "title": "t", "branch": "lot/42-x", "assignee": "author"}
    MARKS = [{"id": 1, "kind": "frozen", "sha": FROZEN, "at_ts": 1.0}]

    def verdicts(self, base):
        with mock.patch.object(merge_queue, "_class_base", return_value=base) as read, \
                mock.patch.object(merge_queue, "pinned", return_value=FROZEN), \
                mock.patch.object(fire.lot_class, "of_lot",
                                  return_value={"class": "light"}) as of_lot, \
                mock.patch.object(merge_queue, "review_gate",
                                  return_value={"ok": True}):
            fire.ready_verdicts(None, None, self.ROW, self.MARKS, repo="/repo",
                                project=None, target="main")
        return read, of_lot

    def test_origin_is_read_first(self):
        read, of_lot = self.verdicts("origin/main")
        read.assert_called_once_with("/repo", "main")
        self.assertEqual(of_lot.call_args.kwargs["base"], "origin/main")

    def test_the_local_target_is_the_fallback(self):
        """Un dépôt sans `origin/<cible>` (clone local, test) garde sa cible."""
        _read, of_lot = self.verdicts(None)
        self.assertEqual(of_lot.call_args.kwargs["base"], "main")

    def test_without_a_repository_the_target_is_kept(self):
        with mock.patch.object(merge_queue, "_class_base") as read, \
                mock.patch.object(fire.lot_class, "of_lot",
                                  return_value={"class": "light"}) as of_lot, \
                mock.patch.object(merge_queue, "review_gate",
                                  return_value={"ok": True}):
            fire.ready_verdicts(None, None, self.ROW, self.MARKS, repo=None,
                                project=None, target="main")
        read.assert_not_called()
        self.assertEqual(of_lot.call_args.kwargs["base"], "main")


if __name__ == "__main__":
    unittest.main()
