# SPDX-License-Identifier: AGPL-3.0-only
"""#2542 : les contrôles statiques passent avant le gel, pas seulement en CI
de paquet.

Témoins : le script d'ameesh (`scripts/freeze-checks.py` : chaque test nommé
existe, un contrôle rouge rend 1 et montre sa sortie, une erreur de syntaxe
rougit la compilation, hors d'un arbre ameesh 2) ; le garde
(`ameesh.freeze_checks` : arbre sans script, vert, rouge, arbre du commit et
non arbre de travail, délai dépassé, résolution du dépôt) ; puis sur Postgres
la CLI `work milestone … frozen` : refus sans jalon, passage outre motivé dans
la note, vert, options refusées hors d'un gel."""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import freeze_checks, registry, work

from .support import PgTestCase

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "freeze_checks_script", REPO / "scripts" / "freeze-checks.py")
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

GREEN = "print('compilation ok')\n"
RED = "import sys\nprint('attribution : faute')\nsys.exit(1)\n"


def _git(root, *args) -> str:
    return subprocess.run(["git", "-C", root, *args], check=True, env=ENV,
                          capture_output=True, text=True).stdout.strip()


class Repo:
    """Un dépôt jetable ; `commit(script=…)` écrit `scripts/freeze-checks.py`."""

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ameesh-2542-")
        subprocess.run(["git", "init", "-q", "-b", "main", self.root], check=True, env=ENV)

    def commit(self, *, script: str | None = None, path: str = "lot.txt",
               text: str = "lot\n") -> str:
        files = {path: text}
        if script is not None:
            files[freeze_checks.SCRIPT] = script
        for rel, content in files.items():
            full = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)
            _git(self.root, "add", rel)
        _git(self.root, "commit", "-q", "-m", "c")
        return _git(self.root, "rev-parse", "HEAD")

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class ScriptTest(unittest.TestCase):
    """Le script d'ameesh : ses contrôles, son verdict, sa sortie."""

    def tree(self) -> str:
        root = tempfile.mkdtemp(prefix="ameesh-2542-arbre-")
        self.addCleanup(shutil.rmtree, root, True)
        for rel in ("src/ameesh", "scripts", "tests"):
            os.makedirs(os.path.join(root, rel))
        return root

    def main(self, root: str, checks=None) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(script.os, "getcwd", return_value=root), \
                mock.patch.object(script, "CHECKS", checks or script.CHECKS), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = script.main([])
        return code, out.getvalue()

    def test_chaque_test_nomme_existe(self):
        """Un contrôle renommé ou retiré de la suite ne passe pas en silence."""
        nommes = [arg for _nom, args in script.CHECKS if "unittest" in args
                  for arg in args if arg.startswith("tests.")]
        self.assertTrue(nommes)
        for ident in nommes:
            module, classe = ident.rsplit(".", 1)
            fichier = REPO.joinpath(*module.split(".")).with_suffix(".py")
            self.assertTrue(fichier.is_file(), ident)
            classes = {n.name for n in ast.parse(fichier.read_text(encoding="utf-8")).body
                       if isinstance(n, ast.ClassDef)}
            self.assertIn(classe, classes, ident)

    def test_rouge_rend_un_et_montre_sa_sortie(self):
        code, out = self.main(self.tree(), (
            ("vert", ["-c", "pass"]),
            ("rouge", ["-c", "import sys; print('la faute'); sys.exit(3)"])))
        self.assertEqual(code, 1)
        self.assertIn("ok  vert", out)
        self.assertIn("RED rouge", out)
        self.assertIn("    la faute", out)

    def test_tous_verts_rend_zero(self):
        code, out = self.main(self.tree(), (("vert", ["-c", "pass"]),))
        self.assertEqual(code, 0)
        self.assertNotIn("RED", out)

    def test_erreur_de_syntaxe_rougit_la_compilation(self):
        root = self.tree()
        pathlib.Path(root, "tests", "test_casse.py").write_text("def (:\n", encoding="utf-8")
        compilation = [c for c in script.CHECKS if c[0] == "compilation"]
        code, out = self.main(root, tuple(compilation))
        self.assertEqual(code, 1)
        self.assertIn("RED compilation", out)
        self.assertIn("test_casse.py", out)

    def test_hors_d_un_arbre_ameesh(self):
        root = tempfile.mkdtemp(prefix="ameesh-2542-vide-")
        self.addCleanup(shutil.rmtree, root, True)
        code, _out = self.main(root)
        self.assertEqual(code, 2)


class GuardTest(unittest.TestCase):
    """`freeze_checks` : le script du commit gelé, sur l'arbre de ce commit."""

    def setUp(self) -> None:
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)

    def test_sans_script_rien_a_passer(self):
        sha = self.repo.commit()
        self.assertEqual(freeze_checks.run(self.repo.root, sha), {"result": "none"})

    def test_vert(self):
        sha = self.repo.commit(script=GREEN)
        result = freeze_checks.run(self.repo.root, sha)
        self.assertEqual(result["result"], "green")
        self.assertIn("compilation ok", result["output"])

    def test_rouge_refuse_avec_la_sortie_et_l_echappatoire(self):
        sha = self.repo.commit(script=RED)
        with self.assertRaises(freeze_checks.Refused) as ctx:
            freeze_checks.run(self.repo.root, sha)
        self.assertEqual(ctx.exception.cause, "checks-red")
        self.assertIn("attribution : faute", str(ctx.exception))
        self.assertIn("--no-static-checks", str(ctx.exception))
        self.assertIsInstance(ctx.exception, work.WorkError)

    def test_arbre_du_commit_et_non_arbre_de_travail(self):
        """Une correction non commitée ne rend pas vert le commit gelé."""
        sha = self.repo.commit(script=RED)
        pathlib.Path(self.repo.root, freeze_checks.SCRIPT).write_text(GREEN, encoding="utf-8")
        pathlib.Path(self.repo.root, "non-suivi.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(freeze_checks.Refused) as ctx:
            freeze_checks.run(self.repo.root, sha)
        self.assertEqual(ctx.exception.cause, "checks-red")
        # et le script voit l'arbre du commit, sans le fichier non suivi
        sha = self.repo.commit(script="import os, sys\n"
                                      "sys.exit(1 if os.path.exists('non-suivi.txt') else 0)\n")
        self.assertEqual(freeze_checks.run(self.repo.root, sha)["result"], "green")

    def test_delai_depasse_refuse(self):
        sha = self.repo.commit(script="import time\ntime.sleep(120)\n")
        with self.assertRaises(freeze_checks.Refused) as ctx:
            freeze_checks.run(self.repo.root, sha, timeout=1)
        self.assertEqual(ctx.exception.cause, "checks-incomplete")

    def test_resolution_du_depot(self):
        sha = self.repo.commit()
        ailleurs = tempfile.mkdtemp(prefix="ameesh-2542-ailleurs-")
        self.addCleanup(shutil.rmtree, ailleurs, True)
        with mock.patch.object(freeze_checks.os, "getcwd", return_value=ailleurs):
            # ni le répertoire courant, ni de dépôt de projet : rien à contrôler
            self.assertEqual(freeze_checks.resolve(sha), (None, None))
            # le dépôt du projet du lot porte le SHA
            self.assertEqual(freeze_checks.resolve(sha, project_repo=self.repo.root),
                             (self.repo.root, sha))
            # un SHA abrégé se lit en entier
            self.assertEqual(freeze_checks.resolve(sha[:12], project_repo=self.repo.root),
                             (self.repo.root, sha))
            self.assertEqual(freeze_checks.resolve("a" * 40, project_repo=self.repo.root),
                             (None, None))
            # un --repo nommé qui ne porte pas le SHA refuse
            with self.assertRaises(freeze_checks.Refused) as ctx:
                freeze_checks.resolve("a" * 40, repo=self.repo.root)
            self.assertEqual(ctx.exception.cause, "sha-unknown")
        with mock.patch.object(freeze_checks.os, "getcwd", return_value=self.repo.root):
            self.assertEqual(freeze_checks.resolve(sha), (self.repo.root, sha))


class CliTest(PgTestCase):
    """Le seul appelant : `ameesh work milestone <lot> frozen`."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)
        registry.upsert(self.db, "author", harness="deepseek", host="pc")
        self.lot = work.add(self.db, title="un lot", assignee="author")["id"]

    def freeze(self, sha, *extra):
        return self.mesh("work", "milestone", str(self.lot), "frozen", "--sha", sha,
                         "--actor", "author", "--repo", self.repo.root, *extra)

    def frozen(self) -> list:
        return [m for m in work.milestones(self.db, self.lot) if m["kind"] == "frozen"]

    def test_rouge_refuse_le_gel_sans_jalon(self):
        proc = self.freeze(self.repo.commit(script=RED))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("freeze refused (checks-red)", proc.stderr)
        self.assertIn("attribution : faute", proc.stderr)
        self.assertEqual(self.frozen(), [])

    def test_passer_outre_demande_une_raison_ecrite_dans_la_note(self):
        sha = self.repo.commit(script=RED)
        proc = self.freeze(sha, "--no-static-checks", "  ")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.frozen(), [])
        proc = self.freeze(sha, "--no-static-checks", "incident #1", "--note", "regel")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.frozen()[0]["note"], "regel — static checks skipped: incident #1")

    def test_vert_gele_et_le_dit(self):
        sha = self.repo.commit(script=GREEN)
        proc = self.freeze(sha)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("static checks: green at %s" % sha[:12], proc.stdout)
        self.assertIn("compilation ok", proc.stdout)
        self.assertEqual([m["sha"] for m in self.frozen()], [sha])

    def test_sans_depot_qui_porte_le_sha_le_gel_passe_et_le_dit(self):
        proc = self.mesh("work", "milestone", str(self.lot), "frozen", "--sha", "a" * 40,
                         "--actor", "author")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("static checks not run", proc.stderr)
        self.assertEqual(len(self.frozen()), 1)

    def test_options_du_gel_refusees_hors_gel(self):
        proc = self.mesh("work", "milestone", str(self.lot), "verdict", "ok", "--sha", "a" * 40,
                         "--actor", "rev", "--no-static-checks", "x")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("only apply to a freeze", proc.stderr)


if __name__ == "__main__":
    unittest.main()
