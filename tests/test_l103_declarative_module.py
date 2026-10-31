# SPDX-License-Identifier: AGPL-3.0-only
"""L103 T2 (#4396): the declarative module in the registry.

A valid `Pipeline` card of the canon is a module of source ``canon``: no code
is imported, the descriptor stores no type (``{types: [], schemas: {}, rules:
[]}``); L103 T3 also exposes the core CLI. The canon selects it by name
(`extensions.ameesh.projects`) like an installed module; `canon check` refuses
an unknown name, a card taking an installed module's name, an invalid card
that a project selects, and two modules claiming one type. `ameesh modules`
reads the canon by default.
"""
from __future__ import annotations

import contextlib
import io
import json
import types as types_mod
import unittest
from unittest import mock

from tests import support  # isolate host config before product imports
from tests.support import PgTestCase
from ameesh import canon, modules
from ameesh.modules import registry
from ameesh.modules.storage import ModuleStore


class FakeEntryPoint:
    group = "ameesh.modules"

    def __init__(self, module, name="sample"):
        self.name = name
        self.module = module
        self.value = "sample_module"
        self.dist = None

    def load(self):
        return self.module


class FakeEntryPoints(list):
    def select(self, group=None):
        return [point for point in self if group in (None, point.group)]


def points(*entries):
    return mock.patch("ameesh.modules.metadata.entry_points",
                      return_value=FakeEntryPoints(entries))


def sample(types=("docs",), name="sample"):
    module = types_mod.ModuleType("sample_module")
    module.NAME = name
    module.descriptor = lambda options=None: {"types": list(types),
                                              "schemas": {}, "rules": []}
    return module


def card(name, *, valid=True, error="", path=None):
    path = path or "pipelines/%s.md" % name
    fiche = canon.Fiche("Pipeline", name, "acme", path,
                        "acme:%s@%s" % (path, "a" * 40), {})
    return canon.Pipeline(fiche, name=name, valid=valid, error=error)


def canon_with(*cards, projects=None):
    federation = {"extensions": {"ameesh": {"projects": projects or {}}}}
    return canon.Canon(root=".", readable=True, id="test", federation=federation,
                       pipelines=list(cards))


def codes(findings, severity=canon.ERROR):
    return {finding.code for finding in findings if finding.severity == severity}


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        patcher = points()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_cards_become_two_canon_modules(self):
        c = canon_with(card("livraison"), card("support"))
        found = registry.discover(canon=c)
        self.assertEqual(found.refused, ())
        for name in ("livraison", "support"):
            module = found.get(name)
            self.assertEqual((module.source, module.storage, module.capabilities,
                              module.cli, module.version),
                             ("canon", (), ("lifecycle", "cli"), "ameesh module %s" % name, None))
            self.assertEqual(module.card_ref,
                             "acme:pipelines/%s.md@%s" % (name, "a" * 40))
            self.assertEqual(module.descriptor_sha256, modules._digest(
                {"types": [], "schemas": {}, "rules": []}))
            self.assertEqual(module.as_dict()["card_ref"], module.card_ref)
        # the installed modules are still there, without a card reference
        self.assertIsNone(found.get("it").card_ref)

    def test_without_a_canon_nothing_changes(self):
        self.assertIsNone(registry.discover().get("livraison"))
        self.assertEqual([m.source for m in registry.discover().modules],
                         ["builtin", "builtin", "builtin"])

    def test_the_bare_profile_stays_empty_and_imports_nothing(self):
        with mock.patch("ameesh.modules.registry._builtin", return_value={}):
            self.assertEqual(registry.discover(builtin=False).modules, ())
            found = registry.discover(builtin=False, canon=canon_with(card("livraison")))
        self.assertEqual(found.names(), ["livraison"])
        self.assertIsInstance(registry._discover(builtin=False,
                                                 canon=canon_with(card("x")))[0]["x"],
                              registry.PipelineModule)

    def test_an_invalid_card_is_refused(self):
        found = registry.discover(canon=canon_with(card("cassee", valid=False,
                                                        error="pipeline-invalid")))
        self.assertIsNone(found.get("cassee"))
        [row] = found.refused
        self.assertEqual((row["name"], row["source"], row["reason"]),
                         ("cassee", "canon", "invalid"))
        self.assertIn("pipeline-invalid", row["error"])

    def test_a_card_never_shadows_an_installed_module(self):
        found = registry.discover(canon=canon_with(card("it")))
        self.assertEqual(found.get("it").source, "builtin")
        [row] = found.refused
        self.assertEqual((row["reason"], row["source"]), ("duplicate", "canon"))
        with self.assertRaisesRegex(ValueError, "duplicate module: 'it'"):
            modules.installed(canon_with(card("it")))

    def test_installed_adds_the_cards_as_pipeline_modules(self):
        found = modules.installed(canon_with(card("livraison")))
        self.assertEqual(sorted(found), ["dev", "it", "livraison", "work-types"])
        self.assertEqual(found["livraison"].descriptor({}),
                         {"types": [], "schemas": {}, "rules": []})
        self.assertEqual(found["livraison"].CARD_REF,
                         "acme:pipelines/livraison.md@%s" % ("a" * 40))
        self.assertEqual(sorted(modules.installed()), ["dev", "it", "work-types"])


class ActivationTest(unittest.TestCase):
    def setUp(self):
        patcher = points()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_project_activates_a_card_by_name(self):
        c = canon_with(card("livraison"),
                       projects={"acme": {"modules": ["it", "livraison"]}})
        rows = {row["module"]: row for row in modules.declarations(c.federation, c)}
        self.assertEqual(sorted(rows), ["it", "livraison"])
        self.assertEqual((rows["livraison"]["types"], rows["livraison"]["schemas"],
                          rows["livraison"]["rules"], rows["livraison"]["scope"]),
                         ([], {}, [], "project"))
        self.assertEqual(codes(canon.validate(c)) & {"project-modules-invalid",
                                                     "pipeline-module-duplicate"},
                         set())

    def test_without_the_canon_the_card_name_is_unknown(self):
        c = canon_with(card("livraison"),
                       projects={"acme": {"modules": ["livraison"]}})
        with self.assertRaisesRegex(ValueError, "unknown installed module: livraison"):
            modules.declarations(c.federation)

    def test_canon_check_refuses_an_unknown_name(self):
        c = canon_with(projects={"acme": {"modules": ["absent"]}})
        found = canon.validate(c)
        error = next(f for f in found if f.code == "project-modules-invalid")
        self.assertIn("unknown installed module: absent", error.message)

    def test_canon_check_refuses_an_activated_invalid_card(self):
        c = canon_with(card("cassee", valid=False, error="bloc fautif"),
                       projects={"acme": {"modules": ["cassee"]}})
        found = canon.validate(c)
        error = next(f for f in found if f.code == "project-modules-invalid")
        self.assertIn("activated but refused", error.message)

    def test_an_inactive_invalid_card_is_not_reported_twice(self):
        c = canon_with(card("cassee", valid=False, error="bloc fautif"))
        found = canon.validate(c)
        self.assertNotIn("module-refused", codes(found, canon.WARNING))
        self.assertNotIn("pipeline-module-duplicate", codes(found))

    def test_canon_check_refuses_a_card_named_like_an_installed_module(self):
        for projects in ({}, {"acme": {"modules": ["it"]}}):
            with self.subTest(projects=projects):
                found = canon.validate(canon_with(card("it"), projects=projects))
                duplicate = next(f for f in found
                                 if f.code == "pipeline-module-duplicate")
                self.assertEqual((duplicate.severity, duplicate.pipeline,
                                  duplicate.path), (canon.ERROR, "it", "pipelines/it.md"))
                # a Pipeline finding blocks no agent (L103 T1)
                self.assertNotIn(duplicate, canon.blocking(found).global_errors)

    def test_two_cards_of_one_name_are_reported_once_as_pipeline_duplicate(self):
        c = canon_with(card("x", path="pipelines/x.md"),
                       card("x", path="pipelines/y.md"))
        found = canon.validate(c)
        self.assertIn("pipeline-duplicate", codes(found))
        self.assertNotIn("pipeline-module-duplicate", codes(found))
        self.assertIsNone(registry.discover(canon=c).get("x"))

    def test_canon_check_refuses_two_modules_claiming_one_type(self):
        c = canon_with(card("livraison"),
                       projects={"acme": {"modules": ["it", "livraison", "sample"]}})
        with points(FakeEntryPoint(sample(types=("release",)))):
            found = canon.validate(c)
        error = next(f for f in found if f.code == "project-modules-invalid")
        self.assertIn("conflicting module types for project acme", error.message)

    def test_a_typeless_installed_module_stays_unselectable(self):
        c = canon_with(projects={"acme": {"modules": ["dev"]}})
        with self.assertRaisesRegex(ValueError, "types must be unique non-empty"):
            modules.declarations(c.federation, c)


class ModulesCommandTest(unittest.TestCase):
    def setUp(self):
        patcher = points()
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_modules(self, argv, canons):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("ameesh.config.load", return_value=object()), \
                mock.patch("ameesh.canon.load_configured", return_value=canons), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = registry.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_modules_lists_the_cards_of_the_canon_by_default(self):
        c = canon_with(card("livraison"), card("support"))
        code, out, _err = self.run_modules(["--json"], [c])
        self.assertEqual(code, 0)
        rows = {row["name"]: row for row in json.loads(out)["modules"]}
        self.assertEqual(sorted(rows), ["dev", "it", "livraison", "support", "work-types"])
        self.assertEqual((rows["support"]["source"], rows["support"]["storage"],
                          rows["support"]["card_ref"]),
                         ("canon", {"types": []},
                          "acme:pipelines/support.md@%s" % ("a" * 40)))
        code, out, _err = self.run_modules([], [c])
        self.assertIn("livraison", out)
        self.assertIn("canon", out)

    def test_no_canon_lists_the_installed_modules_only(self):
        code, out, _err = self.run_modules(["--json", "--no-canon"],
                                           [canon_with(card("livraison"))])
        self.assertEqual(code, 0)
        self.assertEqual([row["name"] for row in json.loads(out)["modules"]],
                         ["dev", "it", "work-types"])

    def test_an_unreadable_canon_only_warns(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("ameesh.config.load", return_value=object()), \
                mock.patch("ameesh.canon.load_configured",
                           side_effect=RuntimeError("git down")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = registry.main(["--json"])
        self.assertEqual(code, 0)
        self.assertIn("canon not read (git down)", err.getvalue())
        self.assertEqual([row["name"] for row in json.loads(out.getvalue())["modules"]],
                         ["dev", "it", "work-types"])


class DeclarativeStorageTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM project_modules")
        patcher = points()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sync_stores_a_typeless_row_and_doctor_knows_the_card(self):
        c = canon_with(card("livraison"),
                       projects={"acme": {"modules": ["it", "livraison"]}})
        modules.sync(self.db, c)
        stored = {row["module"]: row for row in ModuleStore(self.db).stored()}
        self.assertEqual(stored["livraison"]["types"], [])
        ok, lines = modules.doctor_lines(self.db, [c])
        self.assertTrue(ok, lines)
        self.assertIn("no problem", "\n".join(lines))
        payload = modules.active(self.db, [c])
        self.assertEqual([(row["module"], row["scope"]) for row in payload["active"]],
                         [("it", "project"), ("livraison", "project")])
        # without its canon, the stored card is not an installed module
        ok, lines = modules.doctor_lines(self.db, [])
        self.assertFalse(ok)
        self.assertIn("active module livraison is not installed", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
