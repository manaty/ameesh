# SPDX-License-Identifier: AGPL-3.0-only
"""Les lanceurs du dépôt (bin/) démarrent le paquet `ameesh`, sans base.

Chaque lanceur est appelé avec `--help`, depuis un autre dossier, sans
PYTHONPATH hérité : il doit poser lui-même `src/` et viser un module qui
existe (régression : bin/ameesh, bin/agent-mail et bin/agent-runner visaient
encore l'ancien paquet `agent_mesh`).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHERS = ("ameesh", "agent-mail", "agent-runner", "ameesh-approve")


class BinLaunchersTest(unittest.TestCase):
    def test_chaque_lanceur_repond_a_help(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items()
                   if k != "PYTHONPATH" and not k.startswith(("AMEESH_", "AGENT_MESH_"))}
            env["AMEESH_PYTHON"] = sys.executable
            env["AMEESH_CONFIG"] = os.path.join(tmp, "absente.json")
            # aucune base : un DSN injoignable ferait échouer un lanceur qui y toucherait
            env["AMEESH_DSN"] = "postgresql://nobody@127.0.0.1:9/none"
            for name in LAUNCHERS:
                with self.subTest(lanceur=name):
                    proc = subprocess.run(
                        [os.path.join(REPO, "bin", name), "--help"], cwd=tmp, env=env,
                        capture_output=True, text=True, timeout=60)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertNotIn("No module named", proc.stderr)
                    self.assertTrue(proc.stdout.strip(), "aide vide")


if __name__ == "__main__":
    unittest.main()
