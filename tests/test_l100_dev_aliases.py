# SPDX-License-Identifier: AGPL-3.0-only
"""L100 (T2, #4383) — the legacy imports share the moved implementation.

The old path (`ameesh.plan_git`) is declared an alias: both names resolve to
the same module object, so importers and tests that patch a module global by
the old name keep working unchanged (L104 removes the alias).
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from tests import support  # noqa: F401 - isolate host config before product imports
from ameesh import plan_git


class PlanGitAliasTest(unittest.TestCase):
    def test_plan_git_ancien_et_nouveau_nom_sont_le_meme_module(self):
        old = importlib.import_module("ameesh.plan_git")
        new = importlib.import_module("ameesh.modules.dev.plan_git")
        self.assertIs(old, new)
        self.assertIs(sys.modules["ameesh.plan_git"], new)
        self.assertIs(plan_git, new)

    def test_patch_par_l_ancien_nom_est_vu_par_le_nouveau(self):
        new = importlib.import_module("ameesh.modules.dev.plan_git")
        original = new.latest_freeze
        with mock.patch("ameesh.plan_git.latest_freeze", return_value="patched") as patched:
            self.assertIs(new.latest_freeze, patched)
            self.assertEqual(new.latest_freeze([]), "patched")
        self.assertIs(new.latest_freeze, original)

    def test_symboles_importes_par_l_ancien_chemin(self):
        new = importlib.import_module("ameesh.modules.dev.plan_git")
        from ameesh.plan_git import (WORK_TRAILER_RE, final_lots,  # noqa: F401
                                     partial_lots, short_branch)
        self.assertIs(WORK_TRAILER_RE, new.WORK_TRAILER_RE)
        self.assertIs(final_lots, new.final_lots)
        self.assertIs(partial_lots, new.partial_lots)
        self.assertIs(short_branch, new.short_branch)

    def test_ordres_d_import(self):
        names = ("ameesh.plan_git", "ameesh.modules.dev.plan_git",
                 "ameesh.plan_github", "ameesh.resolutions", "ameesh.fire")
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root / "src"))
        for first in names:
            with self.subTest(first=first):
                code = (
                    "import importlib; "
                    f"importlib.import_module({first!r}); "
                    "from ameesh import plan_git; "
                    "from ameesh.modules.dev import plan_git as moved; "
                    "assert plan_git is moved; "
                    "assert moved.WORK_TRAILER_RE is plan_git.WORK_TRAILER_RE"
                )
                result = subprocess.run([sys.executable, "-c", code], cwd=root,
                                        env=env, capture_output=True, text=True,
                                        timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
