# SPDX-License-Identifier: AGPL-3.0-only
"""#6609 : `tests/__init__.py` met l'arbre `src` en tête et refuse un ameesh d'ailleurs.

Sans PYTHONPATH, `python -m unittest tests.…` prenait l'ameesh du venv
installé : faux rouges des tests ciblés d'agent (paquet-horaire refusait une
bibliothèque « déjà chargée ailleurs »).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "src").resolve()


def run(code, pythonpath=None):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=60)


class TestsSysPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # un ameesh « installé » ailleurs, comme celui du venv de l'hôte
        fake = Path(self.tmp.name) / "ameesh"
        fake.mkdir()
        (fake / "__init__.py").write_text("")
        self.elsewhere = self.tmp.name

    def test_sans_pythonpath_ameesh_vient_de_src(self):
        done = run("import tests, ameesh; print(ameesh.__file__)")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(Path(done.stdout.strip()).resolve().is_relative_to(SRC),
                        done.stdout)

    def test_ameesh_d_ailleurs_sur_le_chemin_est_masque(self):
        done = run("import tests, ameesh; print(ameesh.__file__)",
                   pythonpath=self.elsewhere)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(Path(done.stdout.strip()).resolve().is_relative_to(SRC),
                        done.stdout)

    def test_ameesh_deja_charge_ailleurs_arrete_net(self):
        done = run("import ameesh; import tests", pythonpath=self.elsewhere)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("hors de l'arbre", done.stderr)

    def test_tests_en_lien_symbolique_charge_le_src_voisin_du_lien(self):
        root = Path(self.tmp.name) / "copie"
        shutil.copytree(SRC, root / "src",
                        ignore=shutil.ignore_patterns("__pycache__"))
        os.symlink(REPO / "tests", root / "tests")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        done = subprocess.run(
            [sys.executable, "-c", "import tests, ameesh; print(ameesh.__file__)"],
            cwd=root, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(Path(done.stdout.strip()).resolve().is_relative_to(
            (root / "src").resolve()), done.stdout)


if __name__ == "__main__":
    unittest.main()
