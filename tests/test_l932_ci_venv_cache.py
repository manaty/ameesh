# SPDX-License-Identifier: AGPL-3.0-only
"""#932 : le cache du venv de CI est par runner, et un venv restauré est vérifié
avant de servir.

Sans base : lecture du workflow `.github/workflows/tests.yml`. Le venv `.venv-ci`
d'un runner auto-hébergé contient un lien absolu vers le Python de ce runner
(`/home/runner/r<X>/_work/_tool/Python/...`) : restauré par un autre runner, il
sort en exit 127 (run de la PR #618, part psycopg 1/4, 50 s). Deux gardes : clé
de cache par `runner.name`, et contrôle du venv restauré (import des
dépendances) qui le reconstruit s'il est inutilisable.
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")


class VenvCacheTest(unittest.TestCase):

    def test_cle_de_cache_par_runner(self):
        m = re.search(r"key:\s*venv-([^\n]+)", WORKFLOW)
        self.assertIsNotNone(m, "clé de cache du venv introuvable")
        key = m.group(1)
        self.assertIn("runner.name", key)
        self.assertNotIn("runner.environment", key)
        self.assertIn("hashFiles('pyproject.toml')", key)
        self.assertIn("steps.py.outputs.python-version", key)

    def test_venv_restaure_verifie_avant_usage(self):
        # le pas « dépendances » n'est plus sauté sur un cache-hit : il valide
        # le venv restauré (import des dépendances) et le reconstruit sinon.
        self.assertNotIn("if: steps.venv.outputs.cache-hit != 'true'", WORKFLOW)
        m = re.search(r"name: dépendances \(pilote psycopg\)\n(.*?)\n      - &base",
                      WORKFLOW, re.S)
        self.assertIsNotNone(m, "pas « dépendances » introuvable")
        body = m.group(1)
        self.assertIn("cache-hit", body)
        self.assertIn("import psycopg, cryptography, psutil", body)
        self.assertIn("rm -rf .venv-ci", body)
        self.assertIn("python -m venv .venv-ci", body)

    def test_part_utilise_le_venv(self):
        self.assertIn(".venv-ci/bin/python scripts/test-parts.py run", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
