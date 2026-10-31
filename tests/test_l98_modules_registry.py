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
from ameesh import main as ameesh_main, modules, version, workspaces
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
            self.assertIsNone(registry.get("dev"))
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
    @support.needs_builtin_modules
    def test_it_is_listed_and_still_served_as_before(self):
        with points():
            found = registry.discover()
            installed = modules.installed()
        it = found.get("it")
        self.assertEqual(found.names(), ["dev", "it", "work-types"])
        self.assertEqual((it.source, it.version, it.cli),
                         ("builtin", version(), "ameesh module it"))
        self.assertEqual(it.capabilities, ("lifecycle", "cli", "audit"))
        self.assertEqual(it.storage, tuple(installed["it"].descriptor({})["types"]))
        self.assertEqual(len(it.descriptor_sha256), 64)
        # Same evidence digest as `ameesh module --list`.
        with points():
            proof = {row["name"]: row for row in modules.evidence()}
        self.assertEqual(proof["it"]["descriptor_sha256"], it.descriptor_sha256)

    @support.needs_builtin_modules
    def test_dev_is_listed_as_builtin_tooling_through_the_registry(self):
        """L101 T4: `dev` joins the Module interface, without a work type."""
        with points():
            found = registry.discover()
            installed = modules.installed()
        dev = found.get("dev")
        self.assertIsNotNone(dev)
        self.assertEqual((dev.source, dev.version, dev.cli, dev.entry_point),
                         ("builtin", version(), "ameesh module dev", None))
        # L100 T5 (#4386): `dev` serves the `cli` capability.
        self.assertEqual(dev.capabilities, ("cli", "audit", "workspaces"))
        self.assertEqual(dev.storage, ())
        self.assertEqual(len(dev.descriptor_sha256), 64)
        # The façade the core obtains is the workspaces module, through `dev`.
        self.assertIs(registry.get("dev"), installed["dev"])
        self.assertIs(installed["dev"].workspaces(), workspaces)
        self.assertIn("workspaces", registry.CAPABILITIES)
        self.assertEqual(modules.always_active(), ("dev",))
        self.assertTrue(installed["dev"].ALWAYS_ACTIVE)
        self.assertEqual(installed["dev"].descriptor({}), {
            "types": [], "schemas": {}, "rules": []})

    @support.needs_builtin_modules
    def test_dev_descriptor_takes_no_option_and_declares_no_type(self):
        with points():
            dev = modules.installed()["dev"]
        with self.assertRaisesRegex(ValueError, "unknown dev module option"):
            dev.descriptor({"require_qa": ["promoted"]})

    def test_an_external_empty_type_descriptor_is_still_refused(self):
        """The empty-type tolerance is for trusted built-ins only (L101 T4)."""
        empty = sample(descriptor=lambda options=None: {
            "types": [], "schemas": {}, "rules": []})
        with points(FakeEntryPoint(empty)):
            found = registry.discover()
        self.assertIsNone(found.get("sample"))
        self.assertIn("types must be unique non-empty names",
                      found.refused[0]["error"])

    def test_dev_audit_delegates_the_canon_workspaces_checks(self):
        """L101 T4: `audit` is a thin, read-only, host-scoped Module view."""
        from ameesh import canon
        from ameesh.modules import dev
        missing = canon.Finding("host-work-dir-missing", canon.WARNING,
                                "agent a on host h: no working directory",
                                host="h", agent="a", member="m", path="p")
        unknown = canon.Finding("host-workspaces-unknown", canon.WARNING,
                                "`policy.workspaces.dir`: unknown key", host="h")
        other = canon.Finding("agent-unplaced", canon.WARNING, "not a workspace")
        with mock.patch("ameesh.config.load", return_value=object()), \
                mock.patch("ameesh.canon.load_configured", return_value=[object()]), \
                mock.patch("ameesh.canon.validate",
                           return_value=[missing, unknown, other, missing]):
            findings = dev.audit(mock.Mock(), "acme")
        self.assertEqual([f["rule"] for f in findings],
                         ["host-work-dir-missing", "host-workspaces-unknown"])
        self.assertEqual((findings[0]["type"], findings[0]["module"],
                          findings[0]["project"], findings[0]["agent"],
                          findings[0]["host"]),
                         ("module_deviation", "dev", None, "a", "h"))
        self.assertIn("no working directory", findings[0]["detail"])

    def test_dev_audit_is_empty_without_a_readable_canon(self):
        from ameesh.modules import dev
        with mock.patch("ameesh.config.load", return_value=object()), \
                mock.patch("ameesh.canon.load_configured", return_value=[]):
            self.assertEqual(dev.audit(mock.Mock(), "acme"), [])

    def test_the_bare_profile_audits_no_module(self):
        """No built-in module (bare profile): no always-active hook is invented."""
        from ameesh.modules.storage import ModuleStore
        with points(), mock.patch("ameesh.modules._builtin", return_value={}), \
                mock.patch.object(ModuleStore, "active", return_value=[]), \
                mock.patch("ameesh.modules.dev.audit") as dev_audit:
            self.assertEqual(modules.audit(mock.Mock()), [])
        dev_audit.assert_not_called()

    def test_an_external_entry_point_is_described(self):
        with points(FakeEntryPoint(sample())):
            code, out = run(["modules", "--json"])
        self.assertEqual(code, 0)
        rows = {row["name"]: row for row in json.loads(out)["modules"]}
        self.assertEqual(rows["sample"], {
            "name": "sample", "version": "0.1.0", "source": "external",
            "capabilities": ["lifecycle"], "cli": None, "storage": {"types": ["docs"]},
            "distribution": "ameesh-sample-module", "entry_point": "sample_module",
            "descriptor_sha256": rows["sample"]["descriptor_sha256"],
            "card_ref": None})

    def test_declared_capabilities_are_checked_against_the_module(self):
        with points(FakeEntryPoint(sample(CAPABILITIES=("lifecycle", "cli"),
                                          main=lambda db, argv, actor: 0))):
            self.assertEqual(registry.discover().get("sample").capabilities,
                             ("lifecycle", "cli"))
        with points(FakeEntryPoint(sample(CAPABILITIES=("lifecycle", "audit")))):
            found = registry.discover()
        self.assertIsNone(found.get("sample"))
        self.assertIn("declares audit without providing it", found.refused[0]["error"])

    @support.needs_builtin_modules
    def test_refused_entries_are_listed_and_fail_the_command(self):
        with points(FakeEntryPoint(ImportError("boom"), name="broken"),
                    FakeEntryPoint(sample(name="it"), name="it"),
                    FakeEntryPoint(sample())):
            code, out = run(["modules", "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual([row["name"] for row in payload["modules"]],
                         ["dev", "it", "work-types", "sample"])
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
