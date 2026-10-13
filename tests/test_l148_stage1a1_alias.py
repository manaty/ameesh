# SPDX-License-Identifier: AGPL-3.0-only
"""L148, stage 1a-1 : `grand_livre` → `ledger`, alias de transition.

`ameesh.grand_livre` est un alias `sys.modules` de `ameesh.ledger` : les deux
noms donnent le même objet module (un patch atteint le code renommé), un
`from ameesh.grand_livre import *` garde les cinq constantes publiques, et
`python -m ameesh.grand_livre` garde sa ligne d'usage et son code 2.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

from ameesh import grand_livre, ledger
from tests.support import child_env

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class GrandLivreAliasShimTest(unittest.TestCase):
    def test_shim_is_the_ledger_module(self) -> None:
        """P2 : un seul objet module, pas un second module de ré-export."""
        self.assertIs(grand_livre, ledger)

    def test_patch_lands_on_the_code_global(self) -> None:
        """P2 : patcher `grand_livre.time` atteint le code renommé."""
        with mock.patch.object(grand_livre, "time") as faux:
            self.assertIs(ledger.time, faux)

    def test_star_import_keeps_the_public_constants(self) -> None:
        """P2 : `__all__` ne perd aucun nom public, constantes comprises."""
        namespace: dict = {}
        exec("from ameesh.grand_livre import *", namespace)
        for nom in ("DUP_WINDOW_S", "CUMUL_FACTOR", "CUMUL_MIN_USD",
                    "CODEX_RESUME_AGE_S", "UNKNOWN_MODELS", "CorrectionError",
                    "parse_since"):
            self.assertIn(nom, namespace)

    def test_python_m_grand_livre_keeps_the_usage(self) -> None:
        """P3 : `python -m ameesh.grand_livre` sort en 2 avec la ligne d'usage."""
        env = child_env()
        env["PYTHONPATH"] = os.path.join(RACINE, "src") + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run([sys.executable, "-m", "ameesh.grand_livre"],
                              capture_output=True, text=True, timeout=60,
                              env=env, cwd=RACINE)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage: ameesh cost correct", proc.stderr)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main()
