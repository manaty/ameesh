# SPDX-License-Identifier: AGPL-3.0-only
"""#743 : `ameesh serve --exec-only` doit atteindre le serveur d'exécuteur médié.

`main.py` attrapait `serve` avant la branche L108 : `serve.main` (L84) ne
connaît pas `--exec-only`, donc l'API d'exécuteur médiée (`/api/exec/v1`,
docs/EXECUTEUR-MEDIEE.md, scripts/path-b-trial/trial.py) était inatteignable
depuis la CLI. Le routeur tranche maintenant quand `--exec-only` est présent,
quelle que soit sa position dans les arguments.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ServeExecOnlyRoutingTest(unittest.TestCase):
    """La CLI répartit `serve` entre l'API de lecture (L84) et l'exécuteur (L108)."""

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ameesh.main", "serve", *args],
            capture_output=True, text=True, timeout=60, cwd=ROOT,
        )

    def test_exec_only_reaches_the_mediated_server(self):
        proc = self.run_cli("--exec-only", "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # L'usage du serveur médié (L108), pas celui de l'API de lecture (L84).
        self.assertIn("--exec-only", proc.stdout)
        self.assertIn("--listen", proc.stdout)
        self.assertNotIn("--token-file", proc.stdout)

    def test_plain_serve_still_routes_to_the_read_api(self):
        proc = self.run_cli("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--token-file", proc.stdout)
        self.assertIn("--port", proc.stdout)

    def test_exec_only_is_recognised_after_other_options(self):
        proc = self.run_cli("--listen", "127.0.0.1:0", "--exec-only", "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--exec-only", proc.stdout)
        self.assertNotIn("--token-file", proc.stdout)


if __name__ == "__main__":
    unittest.main()
