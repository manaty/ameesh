# SPDX-License-Identifier: AGPL-3.0-only
"""L98 T2b (#4408): the bare profile at run time (`AMEESH_MODULES=bare`).

The switch empties the built-in registry read by `modules._builtin()` and
`registry.discover`; the core then runs without any module. It is a CI and
test profile, never a host option (decision 2, #4405): `doctor` refuses it.
The `profil-nu` CI job runs this module, test_l98_modules_registry,
test_l317_project_modules and test_generic_core under the switch.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import support  # isolate host config before product imports
from ameesh import modules
from ameesh.modules import registry
from ameesh.modules.storage import ModuleStore

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "canon"


def profile(value):
    env = dict(os.environ)
    env.pop(modules.PROFILE_ENV, None)
    if value is not None:
        env[modules.PROFILE_ENV] = value
    return mock.patch.dict(os.environ, env, clear=True)


@contextlib.contextmanager
def no_entry_points():
    # registry imports _entry_points by name: patch both bindings (#4625).
    with mock.patch("ameesh.modules._entry_points", return_value=()), \
            mock.patch("ameesh.modules.registry._entry_points", return_value=()):
        yield


class SwitchTest(unittest.TestCase):
    def test_unset_keeps_the_built_in_modules(self):
        with profile(None), no_entry_points():
            self.assertEqual(modules.profile(), "default")
            self.assertEqual(registry.discover().names(), ["dev", "it"])
            self.assertEqual(modules.always_active(), ("dev",))

    def test_bare_empties_builtin_and_discover(self):
        with profile("bare"), no_entry_points():
            self.assertEqual(modules.profile(), "bare")
            self.assertEqual(modules._builtin(), {})
            found = registry.discover()
            self.assertEqual((found.modules, found.refused), ((), ()))
            self.assertIsNone(registry.get("dev"))
            self.assertEqual(modules.always_active(), ())
            self.assertEqual(modules.installed(), {})
            self.assertEqual(registry.action_connectors(), {})
            self.assertEqual(registry.delivery_connectors(), {})

    def test_an_unknown_value_keeps_the_default_profile(self):
        with profile("Bare"), no_entry_points():
            self.assertEqual(registry.discover().names(), ["dev", "it"])


class DoctorTest(unittest.TestCase):
    def lines(self):
        with no_entry_points(), mock.patch.object(ModuleStore, "stored", return_value=[]):
            return modules.doctor_lines(mock.Mock())

    def test_doctor_refuses_the_bare_profile_on_a_host(self):
        with profile("bare"):
            ok, lines = self.lines()
        self.assertFalse(ok)
        self.assertTrue(any("bare profile" in line and "never a host" in line
                            for line in lines), lines)

    def test_doctor_warns_on_an_unknown_value(self):
        with profile("nu"):
            ok, lines = self.lines()
        self.assertTrue(ok)
        self.assertTrue(any(line.startswith("warning") and "AMEESH_MODULES='nu'" in line
                            for line in lines), lines)

    def test_doctor_is_silent_on_the_default_profile(self):
        with profile(None):
            ok, lines = self.lines()
        self.assertTrue(ok, lines)
        self.assertFalse(any("profile" in line for line in lines), lines)


class CommandTest(unittest.TestCase):
    """`ameesh modules` and `canon check` under the switch, in a child process."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.config = self.tmp / "config.json"
        self.config.write_text("{}\n", encoding="utf-8")

    def canon(self, federation=None):
        root = self.tmp / "canon"
        shutil.copytree(EXAMPLE, root)
        if federation is not None:
            (root / "federation.yaml").write_text(federation, encoding="utf-8")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        for args in (("init", "-q"), ("add", "-A"), ("commit", "-qm", "exemple")):
            subprocess.run(["git", "-C", str(root), *args], env=env, check=True,
                           capture_output=True, timeout=60)
        return root

    def ameesh(self, *argv):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
                   AMEESH_CONFIG=str(self.config), AMEESH_MODULES="bare")
        return subprocess.run([sys.executable, "-m", "ameesh", *argv], env=env,
                              cwd=self.tmp, capture_output=True, text=True, timeout=120)

    def test_modules_lists_no_module(self):
        done = self.ameesh("modules")
        self.assertEqual((done.returncode, done.stdout), (0, "no module\n"), done.stderr)

    def test_canon_check_passes_on_a_canon_without_extensions(self):
        root = self.canon()
        done = self.ameesh("canon", "check", "--canon", str(root), "--ref", "HEAD",
                           "--host", "banc")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("canon valid — 0 error(s)", done.stdout)

    def test_a_canon_activating_a_built_in_module_is_refused(self):
        root = self.canon('federation: "0.1"\nid: exemple\nmembers: []\n'
                          "extensions:\n  ameesh:\n    projects:\n"
                          "      atelier:\n        modules: [it]\n")
        done = self.ameesh("canon", "check", "--canon", str(root), "--ref", "HEAD",
                           "--host", "banc")
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("unknown installed module: it", done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
