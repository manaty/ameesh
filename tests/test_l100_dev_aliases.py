# SPDX-License-Identifier: AGPL-3.0-only
"""L100 (T1 #4382, T2 #4383) — the legacy imports share the moved implementations.

The old paths (`ameesh.merge_pr`, `ameesh.connectors.git_merge`,
`ameesh.plan_git`) are declared aliases: both names resolve to the same module
object, so importers and tests that patch a module global by the old name keep
working unchanged (L104 removes the aliases). `connectors.get("git-merge")`
reads the implementation through the module registry, not through the old
path.
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
from ameesh import connectors, plan_git
from ameesh.modules import dev, registry


class AliasTest(unittest.TestCase):
    def test_merge_pr_ancien_et_nouveau_nom_sont_le_meme_module(self):
        old = importlib.import_module("ameesh.merge_pr")
        new = importlib.import_module("ameesh.modules.dev.merge_pr")
        self.assertIs(old, new)
        self.assertIs(sys.modules["ameesh.merge_pr"], new)
        from ameesh import merge_pr as from_package
        self.assertIs(from_package, new)

    def test_patch_par_l_ancien_nom_est_vu_par_le_nouveau(self):
        new = importlib.import_module("ameesh.modules.dev.merge_pr")
        original = new._run
        with mock.patch("ameesh.merge_pr._run", return_value="x") as patched:
            self.assertIs(new._run, patched)
            self.assertEqual(new._run("argv"), "x")
        self.assertIs(new._run, original)

    def test_alias_du_connecteur_git_merge(self):
        old = importlib.import_module("ameesh.connectors.git_merge")
        new = importlib.import_module("ameesh.modules.dev.git_merge")
        self.assertIs(old, new)
        self.assertIs(sys.modules["ameesh.connectors.git_merge"], new)
        with mock.patch("ameesh.connectors.git_merge.resolve_gh") as patched:
            self.assertIs(new.resolve_gh, patched)


class RegistryTest(unittest.TestCase):
    def test_connectors_get_lit_par_le_registre(self):
        found = registry.action_connectors()
        self.assertEqual(found, {"git-merge": ("dev", dev.git_merge.GitMergeConnector)})
        connector = connectors.get("git-merge")
        self.assertIsInstance(connector, dev.git_merge.GitMergeConnector)
        self.assertEqual((connector.name, connector.dedupe), ("git-merge", "none"))

    def test_le_module_dev_n_est_pas_un_module_de_cycle_de_vie(self):
        # L101 T4 (#4357): `dev` joins the Module interface as a built-in
        # tooling module. It stays outside the lifecycle: no work item type,
        # so `modules.declarations` refuses it (see test_l317_project_modules).
        module = registry.discover().get("dev")
        self.assertIsNotNone(module)
        self.assertEqual(module.storage, ())
        # L100 T5 (#4386): it serves `ameesh module dev …` (capability cli).
        self.assertEqual(module.capabilities, ("cli", "audit", "workspaces"))

    def test_un_nom_d_action_du_coeur_est_refuse_au_fournisseur(self):
        fake = mock.Mock(ACTION_CONNECTORS={"shell-noop": lambda *a, **k: None})
        with mock.patch.object(registry, "_connector_providers",
                               return_value={"x": fake}):
            with self.assertRaisesRegex(ValueError, "duplicate action connector"):
                registry.action_connectors()

    def test_un_connecteur_d_action_inconnu_nomme_les_connus(self):
        with self.assertRaisesRegex(connectors.ConnectorError,
                                    "unknown connector.*git-merge.*shell-noop"):
            connectors.get("ftp")


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


class DevAliasesTest(unittest.TestCase):
    def test_plan_github_identity(self):
        legacy = importlib.import_module("ameesh.plan_github")
        moved = importlib.import_module("ameesh.modules.dev.plan_github")
        self.assertIs(legacy, moved)
        from ameesh import plan_github
        self.assertIs(plan_github, moved)

    def test_plan_github_patch_through_legacy(self):
        moved = importlib.import_module("ameesh.modules.dev.plan_github")
        with mock.patch("ameesh.plan_github.check_repo", return_value="patched") as patched:
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


if __name__ == "__main__":
    unittest.main()
