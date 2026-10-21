# SPDX-License-Identifier: AGPL-3.0-only
"""#2907 : diagnostiquer psycopg 3.1 et refuser son venv en cache, sans base."""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from . import support  # isole les réglages du poste
from ameesh import cli


class PsycopgDoctorTest(unittest.TestCase):
    def warnings(self, version, remote):
        module = types.ModuleType("psycopg")
        module.__version__ = version
        with mock.patch.dict(sys.modules, {"psycopg": module}):
            return cli.driver_warnings("psycopg", remote)

    def test_version_systeme_trop_ancienne_signalee_meme_en_local(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                warning = "\n".join(self.warnings("3.1.7", remote))
                self.assertIn("psycopg 3.1.7 is too old", warning)
                self.assertIn("Connection.notifies(timeout=)", warning)
                self.assertIn("%s -m pip install 'psycopg[binary]>=3.2'" % sys.executable,
                              warning)

    def test_versions_compatibles_sans_avertissement(self):
        for version in ("3.2.0", "3.2.13", "3.10.0", "4.0.0"):
            with self.subTest(version=version):
                self.assertEqual(self.warnings(version, False), [])
                self.assertEqual(self.warnings(version, True), [])

    def test_repli_psql_conseille_la_bonne_borne(self):
        with mock.patch.dict(sys.modules, {"psycopg": None}):
            self.assertEqual(cli.driver_warnings("psql", False), [])
            warning = "\n".join(cli.driver_warnings("psql", True))
        self.assertIn("psycopg[binary]>=3.2", warning)


class PsycopgCacheTest(unittest.TestCase):
    def test_controle_reel_du_cache_rejette_31_accepte_32_et_versions_suivantes(self):
        workflow = (pathlib.Path(support.REPO) / ".github/workflows/tests.yml").read_text()
        check = re.search(r"if \.venv-ci/bin/python -c '([^']+)'", workflow)
        self.assertIsNotNone(check, "contrôle du venv partagé introuvable")
        # Exécuter le contrôle du workflow avec des modules importables, sans
        # installer de paquet ni lire le psycopg éventuellement présent du poste.
        with tempfile.TemporaryDirectory(prefix="psycopg-cache-") as tmp:
            root = pathlib.Path(tmp)
            for name in ("cryptography", "psutil", "coverage", "yaml"):
                (root / (name + ".py")).write_text("")
            for version, valid in (("3.1.7", False), ("3.2.0", True),
                                   ("3.2.13", True), ("3.10.0", True),
                                   ("4.0.0", True)):
                with self.subTest(version=version):
                    (root / "psycopg.py").write_text("__version__ = %r\n" % version)
                    result = subprocess.run(
                        [sys.executable, "-B", "-S", "-c", check.group(1)],
                        cwd=root, env={**os.environ, "PYTHONPATH": str(root)},
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode == 0, valid,
                                     support.attendu(result.stderr))
            (root / "yaml.py").unlink()
            result = subprocess.run(
                [sys.executable, "-B", "-S", "-c", check.group(1)],
                cwd=root, env={**os.environ, "PYTHONPATH": str(root)},
                capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0, "un import absent invalide toujours le cache")


if __name__ == "__main__":
    unittest.main()
