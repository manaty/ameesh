# SPDX-License-Identifier: AGPL-3.0-only
"""L100: legacy imports share the moved implementation, including patches."""
import importlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from . import support


class DevAliasesTest(unittest.TestCase):
    def test_plan_github_identity(self):
        legacy = importlib.import_module("ameesh.plan_github")
        moved = importlib.import_module("ameesh.modules.dev.plan_github")
        self.assertIs(legacy, moved)
        from ameesh import plan_github
        self.assertIs(plan_github, moved)

    def test_plan_github_patch_through_legacy(self):
        moved = importlib.import_module("ameesh.modules.dev.plan_github")
        with patch("ameesh.plan_github.check_repo", return_value="patched") as patched:
            self.assertEqual(moved.check_repo("owner/repo"), "patched")
            patched.assert_called_once_with("owner/repo")

    def test_plan_github_import_orders(self):
        names = ("ameesh.plan_github", "ameesh.modules.dev.plan_github",
                 "ameesh.plan_git")
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root / "src"))
        for first in names:
            with self.subTest(first=first):
                code = (
                    "import importlib; "
                    f"importlib.import_module({first!r}); "
                    "from ameesh import plan_git, plan_github; "
                    "from ameesh.modules.dev import plan_github as moved; "
                    "assert plan_github is moved; "
                    "assert moved.final_lots is plan_git.final_lots"
                )
                result = subprocess.run([sys.executable, "-c", code], cwd=root,
                                        env=env, capture_output=True, text=True,
                                        timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
