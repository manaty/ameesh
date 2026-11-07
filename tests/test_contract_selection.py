# SPDX-License-Identifier: AGPL-3.0-only
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from ameesh import contract_selection as cs, mechanics
from tests.test_l1616_mechanics import MECHANICS, FEDERATION

SHA = "a" * 40
INVENTORY = ("test_global", "test_storage", "test_other")


def rule(name, paths, modules):
    return dict(id=name, paths=paths, operations=list("AMDRCTU"), modules=modules)


def document(*rules):
    return json.dumps(dict(schema=cs.SCHEMA, rules=list(rules)))


def registry(*extra):
    return cs.compile_registry(document(
        rule("global", [], ["test_global"]),
        rule("storage", ["src/storage/*"], ["test_storage"]), *extra))


class SelectionTests(unittest.TestCase):
    def select(self, changes, reg=None):
        return cs.select(reg or registry(), changes, INVENTORY, commit=SHA)

    def test_universal_and_evidence(self):
        result = self.select([cs.Change("M", "src/storage/db.py")])
        self.assertEqual(result.modules, ("test_global", "test_storage"))
        self.assertFalse(result.full_suite_required)
        self.assertEqual(result.to_dict()["evidence"]["test_storage"],
                         dict(reasons=["rule:storage"], paths=["src/storage/db.py"]))
        self.assertEqual(len(result.registry_digest), 64)

    def test_universal_requires_every_operation(self):
        for operation in sorted(cs.OPERATIONS):
            declaration = rule("global", [], ["test_global"])
            declaration["operations"].remove(operation)
            with self.subTest(missing=operation), self.assertRaisesRegex(
                    cs.RegistryError, "universal rule requires all operations"):
                cs.compile_registry(document(declaration))
        result = self.select([])
        self.assertEqual(result.modules, ("test_global",))
        self.assertFalse(result.full_suite_required)

    def test_non_iterable_inventory_has_named_error(self):
        for inventory in (None, 42, True, object()):
            with self.subTest(inventory=inventory), self.assertRaisesRegex(
                    cs.RegistryError, "module inventory must be iterable"):
                cs.select(registry(), [], inventory, commit=SHA)
        result = cs.select(registry(), [], iter(INVENTORY), commit=SHA)
        self.assertEqual(result.modules, ("test_global",))

    def test_rename_checks_old_and_new_and_delete_checks_old(self):
        result = self.select([cs.Change("R", "new/db.py", "src/storage/db.py")])
        self.assertEqual(result.modules, tuple(sorted(INVENTORY)))
        self.assertIn("unknown-path:new/db.py", result.fallback_reasons)
        deleted = self.select([cs.Change("D", "src/storage/db.py")])
        self.assertFalse(deleted.full_suite_required)
        self.assertIn("test_storage", deleted.modules)

    def test_unknown_and_ambiguous_are_full(self):
        for reg, path, reason in (
            (registry(), "config.yaml", "unknown-path:"),
            (registry(rule("overlap", ["src/*"], ["test_other"])),
             "src/storage/db.py", "ambiguous-path:")):
            with self.subTest(reason=reason):
                result = self.select([cs.Change("A", path)], reg)
                self.assertEqual(result.modules, tuple(sorted(INVENTORY)))
                self.assertIn(reason + path, result.fallback_reasons)

    def test_missing_module_is_not_silently_dropped(self):
        result = self.select([], registry(rule("missing", [], ["test_absent"])))
        self.assertTrue(result.full_suite_required)
        self.assertIn("modules-absent:test_absent", result.fallback_reasons)
        self.assertEqual(result.modules, tuple(sorted(INVENTORY)))

    def test_operation_mismatch_falls_back(self):
        declaration = rule("writes", ["src/*"], ["test_storage"])
        declaration["operations"] = ["A"]
        reg = cs.compile_registry(document(declaration))
        result = self.select([cs.Change("D", "src/file.py")], reg)
        self.assertIn("unknown-path:src/file.py", result.fallback_reasons)
        self.assertTrue(result.full_suite_required)

    def test_invalid_changes_and_empty_selection(self):
        for change in (cs.Change("R", "x"), cs.Change("M", "../x"), cs.Change("X", "x")):
            self.assertIn("invalid-change", self.select([change]).fallback_reasons)
        reg = cs.compile_registry(document(rule("only", ["src/*"], ["test_storage"])))
        self.assertTrue(self.select([], reg).full_suite_required)
        with self.assertRaises(cs.RegistryError):
            cs.select(reg, [], [], commit=SHA)
        with self.assertRaises(cs.RegistryError):
            cs.select(reg, [], INVENTORY, commit="main")

    def test_invalid_registry_and_stable_json_digest(self):
        valid = document(rule("one", [], ["test_global"]))
        self.assertEqual(cs.compile_registry(valid).digest,
                         cs.compile_registry(json.dumps(json.loads(valid), indent=4)).digest)
        for text in ('{}', '{"schema":1,"schema":2}',
                     document(rule("a", ["../x"], ["test_global"])),
                     document(rule("a", [], ["bad;module"])),
                     document(rule("a", [], ["test_global"]), rule("a", [], ["test_other"]))):
            with self.subTest(text=text), self.assertRaises(cs.RegistryError):
                cs.compile_registry(text)


class CanonAndGitTests(unittest.TestCase):
    def test_n_minus_one_and_project_policy(self):
        old = mechanics.compile_document(MECHANICS)
        self.assertIsNone(old.override("ameesh"))
        text = MECHANICS.replace("projects: {}", "projects:\n  ameesh:\n    contract_selection:\n      registry: tests/contracts.json")
        compiled = mechanics.compile_document(text)
        self.assertEqual(compiled.override("ameesh").contract_selection.registry,
                         "tests/contracts.json")
        for path in ("../outside", "/absolute", "tests/*.json"):
            with self.subTest(path=path), self.assertRaises(mechanics.MechanicsError):
                mechanics.compile_document(text.replace("tests/contracts.json", path))

    def test_reads_candidate_blob_not_worktree_and_reports_invalid(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            def git(*args):
                return subprocess.check_output(["git", "-C", tmp, *args], stderr=subprocess.DEVNULL).decode().strip()
            git("init")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Test")
            Path(tmp, "contracts.json").write_text(document(rule("all", [], ["test_global"])))
            Path(tmp, "processus").mkdir()
            text = MECHANICS.replace("projects: {}", "projects:\n  ameesh:\n    contract_selection:\n      registry: contracts.json")
            Path(tmp, "processus/mecanique.yaml").write_text(text)
            Path(tmp, "federation.yaml").write_text(FEDERATION)
            git("add", ".")
            git("commit", "-m", "fixture")
            sha = git("rev-parse", "HEAD")
            policy = mechanics.load(tmp, sha, "ameesh").contract_selection
            self.assertEqual(mechanics.load(tmp, sha, "ameesh").to_dict()["contract_selection"],
                             {"registry": "contracts.json"})
            Path(tmp, "contracts.json").write_text("invalid")
            result = cs.load_and_select(tmp, sha, policy, [], INVENTORY)
            self.assertFalse(result.full_suite_required)
            self.assertEqual(result.modules, ("test_global",))
            git("add", ".")
            git("commit", "-m", "invalid registry")
            result = cs.load_and_select(tmp, git("rev-parse", "HEAD"), policy, [], INVENTORY)
            self.assertTrue(result.full_suite_required)
            self.assertTrue(any(r.startswith("registry-invalid:") for r in result.fallback_reasons))
            absent = cs.load_and_select(tmp, sha, None, [], INVENTORY)
            self.assertIn("policy-absent", absent.fallback_reasons)


if __name__ == "__main__":
    unittest.main()
