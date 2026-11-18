# SPDX-License-Identifier: AGPL-3.0-only
"""#7663 (#7637 tranche 4): the Nexlink release engine is a module.

The core keeps no import of ``ameesh.nexlink_release``: the release model is a
Module capability read through ``registry.release_model``. The bare profile has
no built-in module, hence no Nexlink release model.
"""
from __future__ import annotations

import ast
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest import mock

from tests import support  # isolate host config before product imports
from ameesh import modules, release_cutoff
from ameesh.modules import registry

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ameesh"


@contextlib.contextmanager
def no_entry_points():
    with mock.patch("ameesh.modules._entry_points", return_value=()), \
            mock.patch("ameesh.modules.registry._entry_points", return_value=()):
        yield


class DiscoveryTest(unittest.TestCase):
    def test_the_release_module_is_builtin_tooling(self):
        with no_entry_points():
            found = registry.discover()
        module = found.get("nexlink")
        self.assertIsNotNone(module)
        self.assertEqual((module.source, module.capabilities, module.cli,
                          module.storage, module.release_models),
                         ("builtin", ("release",), None, (), ("nexlink",)))
        self.assertIn("nexlink", modules.always_active())
        self.assertEqual(registry.release_models(), ("nexlink",))

    def test_the_engine_is_loaded_only_when_the_model_is_asked(self):
        script = (
            "import sys\n"
            "from ameesh.modules import registry\n"
            "assert 'ameesh.modules.nexlink.release' not in sys.modules\n"
            "assert registry.release_model('nexlink').__name__ == "
            "'ameesh.modules.nexlink.release'\n"
            "assert 'ameesh.modules.nexlink.release' in sys.modules\n")
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        done = subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_provider_is_the_module_object(self):
        from ameesh.modules.nexlink import release as engine
        self.assertIs(registry.release_model("nexlink"), engine)
        self.assertIsNone(registry.release_model("unknown"))
        self.assertEqual(registry.release_reserved_prefixes(),
                         ("release-attest:", "release-skip:"))

    def test_the_module_declares_no_work_item_type(self):
        module = modules.installed()["nexlink"]
        self.assertEqual(module.descriptor({}),
                         {"types": [], "schemas": {}, "rules": []})
        with self.assertRaisesRegex(ValueError, "unknown nexlink module option"):
            module.descriptor({"types": ["release"]})


class BareProfileTest(unittest.TestCase):
    def test_no_module_means_no_release_model(self):
        with mock.patch.dict(os.environ, {"AMEESH_MODULES": "bare"}), no_entry_points():
            self.assertEqual(registry.release_models(), ())
            self.assertIsNone(registry.release_model("nexlink"))
            with self.assertRaisesRegex(release_cutoff.ReleaseError, "not provided"):
                release_cutoff.release_provider("nexlink")
            self.assertIsNone(release_cutoff.release_provider("semver"))
            cfg = types.SimpleNamespace(humans="", radar={})
            with mock.patch.object(release_cutoff.radar, "settings_of",
                                   return_value=types.SimpleNamespace(projects={})):
                with self.assertRaisesRegex(release_cutoff.ReleaseError, "not provided"):
                    release_cutoff.ProjectRelease.from_config(
                        cfg, "nexlink", {"repo": ".", "target": "develop",
                                         "model": "nexlink"})


class ConfigurationTest(unittest.TestCase):
    def test_the_model_is_accepted_by_its_module_and_keeps_its_shape(self):
        cfg = types.SimpleNamespace(humans="", radar={})
        with mock.patch.object(release_cutoff.radar, "settings_of",
                               return_value=types.SimpleNamespace(projects={})):
            project = release_cutoff.ProjectRelease.from_config(
                cfg, "nexlink", {"repo": ".", "target": "develop", "model": "nexlink"})
            self.assertEqual(project.model, "nexlink")
            with self.assertRaisesRegex(release_cutoff.ReleaseError, "nexlink model"):
                release_cutoff.ProjectRelease.from_config(
                    cfg, "nexlink", {"repo": ".", "target": "main", "model": "nexlink"})
            # An unknown model is a closed refusal, never the semver fallback.
            with self.assertRaisesRegex(release_cutoff.ReleaseError, "not provided"):
                release_cutoff.ProjectRelease.from_config(
                    cfg, "nexlink", {"repo": ".", "model": "unknown"})


class ClassificationTest(unittest.TestCase):
    def test_the_moved_engine_keeps_the_sensitive_release_rule(self):
        """The pinned policy (generated from the canon) must follow the move.

        A future regeneration from a canon that still names the old path would
        drop the engine from the release chain and silently fall back to the
        project default; this witness makes that a red test (#7663, blocking
        verdict from ameesh-cx2)."""
        policy = json.loads((ROOT / "scripts" / "alias-review-classes.json").read_text())
        moved = "src/ameesh/modules/nexlink/release.py"
        old = "src/ameesh/nexlink_release.py"
        release_entries = [entry for entry in policy["classes"].values()
                           if "src/ameesh/release.py" in entry["rule"]]
        self.assertTrue(release_entries)
        self.assertEqual({entry["class"] for entry in release_entries}, {"sensitive"})
        for entry in release_entries:
            self.assertIn(moved, entry["rule"])
            self.assertNotIn(old, entry["rule"])


class CoreImportTest(unittest.TestCase):
    def test_the_core_never_imports_the_engine(self):
        for name in ("release.py", "release_cutoff.py", "work.py", "ci.py"):
            with self.subTest(file=name):
                tree = ast.parse((SOURCE / name).read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        module = node.module or ""
                        self.assertNotIn("nexlink_release", module)
                        for alias in node.names:
                            self.assertNotEqual(alias.name, "nexlink_release")
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            self.assertNotIn("nexlink_release", alias.name)


if __name__ == "__main__":
    unittest.main()
