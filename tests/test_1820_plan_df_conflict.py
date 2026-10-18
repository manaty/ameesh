# SPDX-License-Identifier: AGPL-3.0-only
"""#1820 : le conflit D/F du plan est nommé, jamais `head-moved`.

Repro : un arbre où `dir` est un **fichier** au SHA candidat, et un plan rangé
sous `dir/plan.json` dans l'arbre de travail (qui peut différer du SHA). Le
`ls-tree` exact ne voit pas ce chemin, `update-index` échoue plus tard en
« appears as both a file and as a directory », et cet échec était traduit en
`head-moved` alors que la tête n'avait pas bougé. Le geste refuse désormais
avant toute construction avec `plan-path-conflict` ; `head-moved` reste réservé
à la tête qui a réellement bougé.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from ameesh import prevalidation as pv

A = "a" * 40
B = "b" * 40


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc.stdout.strip()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _plan(head: str = A) -> dict:
    """Un plan L161f minimal que `meets_canon` accepte avec P0/P1."""
    return {
        "schema": pv.PLAN_SCHEMA, "head": head,
        "map": {"commit": B, "used": True, "problem": None, "age_h": 1.0},
        "selection": {"mode": "impacted", "why": "", "modules": 2, "impacted": 2,
                      "contract": 1, "selected": 2, "skipped": 0},
        "sure": True, "phase2_safe": True, "unsure": [], "phase2_why": "",
        "contract": ["test_cluster"],
        "classes": {"test_a": ["P0", "modifié"],
                    "test_cluster": ["P1", "contrat"]},
        "parts": {"psycopg": [{"part": 1, "of": 1,
                               "modules": ["test_a", "test_cluster"],
                               "estimate_s": 10}]},
    }


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _git_runner(head: str = A, file_parent: str = "dir"):
    """Un git simulé : `dir` est un blob au SHA, le plan exact n'existe pas."""
    def run(argv, **kwargs):
        if "rev-parse" in argv:
            return _Proc(0, head + "\n")
        if "ls-tree" in argv:
            paths = argv[argv.index("--") + 1:]
            if paths == [file_parent]:
                return _Proc(0, "100644 blob %s\t%s\0" % (B, file_parent))
            return _Proc(0, "")
        if "check-ignore" in argv:
            return _Proc(1, "")
        return _Proc(0, "")
    return run


class PlanPathConflictTest(unittest.TestCase):
    """Vrai git : le chemin est refusé pour la cause nommée, avant l'arbre."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "tests@ameesh.invalid")
        _git(self.repo, "config", "user.name", "tests")
        _write(os.path.join(self.repo, "dir"), "un fichier, pas un répertoire\n")
        _git(self.repo, "add", "dir")
        _git(self.repo, "commit", "-q", "-m", "dir as file")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        # L'arbre de travail diffère du SHA (le geste ne s'appuie pas sur
        # `git status`) : `dir` y est un répertoire pour que le plan existe.
        os.unlink(os.path.join(self.repo, "dir"))
        os.makedirs(os.path.join(self.repo, "dir"))
        self.plan_rel = "dir/plan.json"
        self.plan = '{"schema": "ameesh-test-plan/1"}\n'
        _write(os.path.join(self.repo, self.plan_rel), self.plan)

    def _plan_path(self, relative: str) -> str:
        return os.path.join(self.repo, *relative.split("/"))

    def test_plan_sous_un_fichier_du_sha_nomme_le_conflit(self):
        with self.assertRaises(pv.PrevalidationError) as caught:
            pv.remote_plan_path(self._plan_path(self.plan_rel), self.repo,
                                head=self.head)
        self.assertEqual(caught.exception.cause, "plan-path-conflict")
        self.assertIn("dir", caught.exception.why)
        self.assertIn("fichier", caught.exception.why)

    def test_conflit_df_n_est_jamais_head_moved(self):
        """Le chemin fautif ne peut plus être dicté `head-moved` : la tête n'a
        pas bougé, l'arbre du SHA + plan n'est simplement pas constructible."""
        with self.assertRaises(pv.PrevalidationError) as caught:
            pv.pinned_snapshot(self.repo, self.head, self.plan_rel, self.plan)
        self.assertEqual(caught.exception.cause, "selection-unsafe")
        self.assertNotEqual(caught.exception.cause, "head-moved")

    def test_plan_qui_remplace_un_fichier_reste_selection_unsafe(self):
        """Non-régression : le chemin exact d'un fichier du SHA garde sa cause."""
        with self.assertRaises(pv.PrevalidationError) as caught:
            pv.remote_plan_path(os.path.join(self.repo, "dir"), self.repo,
                                head=self.head)
        self.assertEqual(caught.exception.cause, "selection-unsafe")

    def test_plan_sous_un_repertoire_du_sha_reste_accepte(self):
        """Non-régression : un parent qui est un arbre n'est pas un conflit."""
        os.unlink(os.path.join(self.repo, self.plan_rel))
        os.rmdir(os.path.join(self.repo, "dir"))
        os.makedirs(os.path.join(self.repo, "dir"))
        _write(os.path.join(self.repo, "dir", "keep.txt"), "keep\n")
        _git(self.repo, "add", "dir/keep.txt")
        _git(self.repo, "commit", "-q", "-m", "dir as tree")
        head = _git(self.repo, "rev-parse", "HEAD")
        _write(os.path.join(self.repo, self.plan_rel), self.plan)
        self.assertEqual(
            pv.remote_plan_path(self._plan_path(self.plan_rel), self.repo, head=head),
            self.plan_rel)


class ValidateRefusalTest(unittest.TestCase):
    """Le geste complet refuse avant l'offload, avec la cause de l'opérateur."""

    def test_validate_refuse_avant_tout_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dir"))
            plan_path = os.path.join(tmp, "dir", "plan.json")
            with open(plan_path, "w", encoding="utf-8") as fh:
                json.dump(_plan(), fh)

            def no_run(*args, **kwargs):
                self.fail("l'offload ne doit pas partir sur un conflit D/F")

            with self.assertRaises(pv.PrevalidationError) as caught:
                pv.validate(
                    lot=1820, plan_path=plan_path, head=A, host="vm1",
                    params=SimpleNamespace(classes=("P0", "P1"), contracts=(),
                                           hosts=("vm1",), timeout_seconds=60),
                    repo=tmp, runner=no_run,
                    snapshotter=lambda *args: self.fail(
                        "l'arbre ne doit pas être construit après le refus"),
                    git_runner=_git_runner())
            self.assertEqual(caught.exception.cause, "plan-path-conflict")
            self.assertIn("dir", caught.exception.why)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
