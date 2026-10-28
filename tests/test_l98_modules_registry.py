# SPDX-License-Identifier: AGPL-3.0-only
"""L98 tranche 1 (#4308): Module interface, registry, `ameesh modules`, bare profile."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import types as types_mod
import unittest
from unittest import mock

from tests import support  # isolate host config before product imports
from ameesh import main as ameesh_main, modules, version
from ameesh.modules import registry

ROOT = Path(__file__).resolve().parents[1]


class FakeDistribution:
    name = "ameesh-sample-module"
    version = "0.1.0"


class FakeEntryPoint:
    group = "ameesh.modules"

    def __init__(self, module, name="sample"):
        self.name = name
        self.module = module
        self.value = "sample_module"
        self.dist = FakeDistribution()

    def load(self):
        if isinstance(self.module, Exception):
            raise self.module
        return self.module


class FakeEntryPoints(list):
    def select(self, group=None):
        return [point for point in self if group in (None, point.group)]


def sample(name="sample", **attributes):
    module = types_mod.ModuleType("sample_module")
    module.NAME = name
    module.descriptor = lambda options=None: {"types": ["docs"], "schemas": {}, "rules": []}
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def points(*entries):
    return mock.patch("ameesh.modules.metadata.entry_points",
                      return_value=FakeEntryPoints(entries))


def bare():
    """The bare profile: no built-in module and no installed entry point."""
    stack = contextlib.ExitStack()
    stack.enter_context(points())
    stack.enter_context(mock.patch("ameesh.modules.registry._builtin", return_value={}))
    return stack


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = ameesh_main._dispatch(argv)
    return code, out.getvalue()


class BareProfileTest(unittest.TestCase):
    def test_the_bare_profile_starts_without_any_module(self):
        with points():
            found = registry.discover(builtin=False)
        self.assertEqual(found.modules, ())
        self.assertEqual(found.refused, ())
        with bare():
            code, out = run(["modules", "--json"])
            self.assertEqual((code, json.loads(out)), (0, {"modules": [], "refused": []}))
            code, out = run(["modules"])
        self.assertEqual((code, out), (0, "no module\n"))

    def test_bare_discovery_imports_no_module_implementation(self):
        script = ("import sys\n"
                  "from unittest import mock\n"
                  "from ameesh.modules import registry\n"
                  "with mock.patch('ameesh.modules._entry_points', return_value=()):\n"
                  "    assert registry.discover(builtin=False).modules == ()\n"
                  "print(sorted(m for m in sys.modules if m.startswith('ameesh.modules.')))\n")
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        done = subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "['ameesh.modules.registry']")


class RegistryTest(unittest.TestCase):
    def test_it_is_listed_and_still_served_as_before(self):
        with points():
            found = registry.discover()
            installed = modules.installed()
        it = found.get("it")
        self.assertEqual(found.names(), ["it"])
        self.assertEqual((it.source, it.version, it.cli),
                         ("builtin", version(), "ameesh module it"))
        self.assertEqual(it.capabilities, ("lifecycle", "cli", "audit"))
        self.assertEqual(it.storage, tuple(installed["it"].descriptor({})["types"]))
        self.assertEqual(len(it.descriptor_sha256), 64)
        # Same evidence digest as `ameesh module --list`.
        with points():
            proof = {row["name"]: row for row in modules.evidence()}
        self.assertEqual(proof["it"]["descriptor_sha256"], it.descriptor_sha256)

    def test_an_external_entry_point_is_described(self):
        with points(FakeEntryPoint(sample())):
            code, out = run(["modules", "--json"])
        self.assertEqual(code, 0)
        rows = {row["name"]: row for row in json.loads(out)["modules"]}
        self.assertEqual(rows["sample"], {
            "name": "sample", "version": "0.1.0", "source": "external",
            "capabilities": ["lifecycle"], "cli": None, "storage": {"types": ["docs"]},
            "distribution": "ameesh-sample-module", "entry_point": "sample_module",
            "descriptor_sha256": rows["sample"]["descriptor_sha256"]})

    def test_declared_capabilities_are_checked_against_the_module(self):
        with points(FakeEntryPoint(sample(CAPABILITIES=("lifecycle", "cli"),
                                          main=lambda db, argv, actor: 0))):
            self.assertEqual(registry.discover().get("sample").capabilities,
                             ("lifecycle", "cli"))
        with points(FakeEntryPoint(sample(CAPABILITIES=("lifecycle", "audit")))):
            found = registry.discover()
        self.assertIsNone(found.get("sample"))
        self.assertIn("declares audit without providing it", found.refused[0]["error"])

    def test_refused_entries_are_listed_and_fail_the_command(self):
        with points(FakeEntryPoint(ImportError("boom"), name="broken"),
                    FakeEntryPoint(sample(name="it"), name="it"),
                    FakeEntryPoint(sample())):
            code, out = run(["modules", "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual([row["name"] for row in payload["modules"]], ["it", "sample"])
        errors = {row["name"]: row["error"] for row in payload["refused"]}
        self.assertIn("cannot be loaded: boom", errors["broken"])
        self.assertEqual(errors["it"], "duplicate installed module: 'it'")



class CoreReadsTest(unittest.TestCase):
    """L98 T3 (#4406): `work add` and `work show` read modules through the registry."""

    def test_admits_reads_the_project_module_types(self):
        db = mock.Mock()
        db.query.return_value = [{"module": "it"}]
        self.assertTrue(registry.admits(db, "acme", "release"))
        self.assertEqual(db.query.call_args[0][1], ("acme", "release"))
        db.query.return_value = []
        self.assertFalse(registry.admits(db, "acme", "release"))

    def test_milestones_reads_the_lot_module_milestones(self):
        db = mock.Mock()
        db.query.return_value = [{"id": 2}, {"id": 1}]
        self.assertEqual(registry.milestones(db, "7"), [{"id": 2}, {"id": 1}])
        self.assertEqual(db.query.call_args[0][1], (7, 100))

if __name__ == "__main__":
    unittest.main()
