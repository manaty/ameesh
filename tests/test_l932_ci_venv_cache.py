# SPDX-License-Identifier: AGPL-3.0-only
"""#932 : le cache du venv de CI est par runner, et un venv restauré est vérifié
avant de servir.

Sans base : lecture du workflow `.github/workflows/tests.yml`. Le venv `.venv-ci`
d'un runner auto-hébergé contient un lien absolu vers le Python de ce runner
(`/home/runner/r<X>/_work/_tool/Python/...`) : restauré par un autre runner, il
sort en exit 127 (run de la PR #618, part psycopg 1/4, 50 s). Deux gardes : une
identité de cache qui distingue les runners auto-hébergés (`runner.name`, #1294
réserve la clé partagée `hosted` aux runners hébergés), et le contrôle du venv
restauré (import des dépendances) qui le reconstruit s'il est inutilisable.

Depuis #1293, la clé primaire est celle du run et la restauration passe par
`restore-keys` : le témoin de ce resauvetage est dans
`tests/test_l1293_l1294_venv_cache.py`, celui-ci garde les gardes de #932.
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
        # #1294 : les runners hébergés (au nom variable) partagent `hosted` ;
        # un runner auto-hébergé garde son nom (venv lié à son Python).
        self.assertIn("runner.environment == 'github-hosted' && 'hosted' || runner.name",
                      key)
        self.assertIn("hashFiles('pyproject.toml')", key)
        self.assertIn("steps.py.outputs.python-version", key)

    def test_venv_restaure_verifie_avant_usage(self):
        # le pas « dépendances » n'est conditionné par aucun cache-hit : il
        # valide le venv restauré (import des dépendances) et le reconstruit
        # sinon, y compris sur une restauration par clé partielle (#1293).
        self.assertNotIn("if: steps.venv.outputs.cache-hit != 'true'", WORKFLOW)
        m = re.search(r"name: dépendances \(pilote psycopg\)\n(.*?)\n      - &base",
                      WORKFLOW, re.S)
        self.assertIsNotNone(m, "pas « dépendances » introuvable")
        body = m.group(1)
        self.assertNotIn("cache-hit", body)
        self.assertIn("import psycopg, cryptography, psutil", body)
        self.assertIn("rm -rf .venv-ci", body)
        self.assertIn("python -m venv .venv-ci", body)

    def test_part_utilise_le_venv(self):
        self.assertIn(".venv-ci/bin/python scripts/test-parts.py run", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
