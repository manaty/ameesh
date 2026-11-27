# SPDX-License-Identifier: AGPL-3.0-only
"""#8366 : les enfants des parts importent les sources sans installation."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent


class ChildPythonPathTest(unittest.TestCase):
    def test_sources_heritees_en_serie_et_en_parallele(self):
        with tempfile.TemporaryDirectory(prefix="parts-8366-") as tmp:
            root = pathlib.Path(tmp)
            (root / "scripts").mkdir()
            (root / "tests").mkdir()
            (root / "tests/__init__.py").touch()
            (root / "src").symlink_to(REPO / "src", target_is_directory=True)
            shutil.copyfile(REPO / "scripts/test-parts.py",
                            root / "scripts/test-parts.py")
            # L'import du module prépare aussi le découpage parallèle. -S
            # empêche un paquet installé de masquer le défaut ; cwd est hors
            # du dépôt, seul l'environnement hérité donne accès aux sources.
            probe = '''import os, pathlib, subprocess, sys, tempfile, unittest
with tempfile.TemporaryDirectory() as cwd:
    result = subprocess.run(
        [sys.executable, '-S', '-c',
         'import ameesh; print(ameesh.__file__)'],
        cwd=cwd, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert str(pathlib.Path(result.stdout.strip()).resolve()) == SOURCE
    paths = os.environ['PYTHONPATH'].split(os.pathsep)
    root = pathlib.Path(__file__).resolve().parent.parent
    assert paths[:2] == [str(root / 'src'), str(root)], paths
    if os.environ.get('INHERITED_8366'):
        assert paths[-1] == os.environ['INHERITED_8366'], paths
class Probe(unittest.TestCase):
    def test_child(self):
        self.assertEqual(result.returncode, 0)
'''.replace("SOURCE", repr(str((REPO / "src/ameesh/__init__.py").resolve())))
            modules = ["test_child_a", "test_child_b"]
            for module in modules:
                (root / "tests" / (module + ".py")).write_text(probe)
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "schema": "ameesh-test-plan/1",
                "parts": {"psql": [{"part": 1, "of": 1,
                    "modules": modules, "estimate_s": 1}]},
            }))
            inherited = root / "inherited"
            inherited.mkdir()
            for parallel in (1, 2):
                for previous in (None, str(inherited)):
                    with self.subTest(parallel=parallel, inherited=previous):
                        env = dict(os.environ, AMEESH_REQUIRE_DB="0",
                                   AMEESH_CI_JOBS_PER_HOST="2")
                        env.pop("AMEESH_TEST_PARALLEL_WORKER", None)
                        env.pop("PYTHONPATH", None)
                        env.pop("INHERITED_8366", None)
                        if previous is not None:
                            env["PYTHONPATH"] = previous
                            env["INHERITED_8366"] = previous
                        done = subprocess.run([
                            sys.executable, str(root / "scripts/test-parts.py"),
                            "run", "--plan", str(plan), "--part", "1",
                            "--driver", "psql", "--parallel", str(parallel)],
                            cwd=root, env=env, capture_output=True, text=True,
                            timeout=45)
                        self.assertEqual(done.returncode, 0,
                                         done.stdout + done.stderr)
