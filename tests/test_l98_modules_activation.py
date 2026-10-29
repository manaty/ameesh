# SPDX-License-Identifier: AGPL-3.0-only
"""L98 T2a (#4407): module activation by federation, team and project.

The canon's three scopes compile to the same per-project rows `project_modules`
stores; no migration. A refused entry point blocks only when a scope activates
it, `ameesh modules --active` reads the applied rows, and the doctor checks
them against the readable canon and the installed descriptors.
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
from ameesh import canon, db as db_mod, modules
from ameesh.modules import registry
from ameesh.modules.storage import ModuleStore


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


def external_points(*points):
    return mock.patch("ameesh.modules.metadata.entry_points",
                      return_value=FakeEntryPoints(points))


def sample(types=None, name="sample"):
    module = types_mod.ModuleType("sample_module")
    module.NAME = name
    declared = list(types or ("docs",))
    module.TYPES = tuple(declared)
    module.descriptor = lambda options=None: {"types": list(declared),
                                              "schemas": {}, "rules": []}
    return module


def federation(*, modules_=None, teams=None, projects=None):
    extension = {}
    if modules_ is not None:
        extension["modules"] = modules_
    if teams is not None:
        extension["teams"] = teams
    if projects is not None:
        extension["projects"] = projects
    return {"extensions": {"ameesh": extension}}


def canon_with(*, packages=("acme",), agents=(), federation_=None, ident="test"):
    """A Canon carrying the team fields the activation resolver reads."""
    packages_ = [types_mod.SimpleNamespace(team=team) for team in packages]
    agents_ = [types_mod.SimpleNamespace(team=team, effective_projects=list(projects))
               for team, projects in agents]
    return canon.Canon(root=".", readable=True, id=ident,
                       federation=federation_ or federation(),
                       packages=packages_, agents=agents_)


def rows_of(entries):
    return [(row["project"], row["module"]) for row in entries]


class ActivationTest(unittest.TestCase):
    def setUp(self):
        discover = external_points()
        discover.start()
        self.addCleanup(discover.stop)

    def test_federation_scope_enables_every_project_the_canon_knows(self):
        c = canon_with(packages=("acme", "beta"),
                       federation_=federation(modules_=["it"],
                                              projects={"gamma": {"modules": []}}))
        entries = modules.declarations(c.federation, c)
        self.assertEqual(rows_of(entries),
                         [("acme", "it"), ("beta", "it"), ("gamma", "it")])
        self.assertTrue(all(row["scope"] == "federation" for row in entries))

    def test_team_scope_covers_the_projects_its_packages_and_agents_carry(self):
        c = canon_with(packages=("ameesh",),
                       agents=[("ameesh", ["ameesh", "nexlink"])],
                       federation_=federation(
                           teams={"ameesh": {"modules": ["it"]}}))
        entries = modules.declarations(c.federation, c)
        self.assertEqual(rows_of(entries),
                         [("ameesh", "it"), ("nexlink", "it")])
        self.assertEqual({(row["scope"], row["team"]) for row in entries},
                         {("team", "ameesh")})

    def test_the_narrowest_scope_wins_the_options_and_the_project_always_wins(self):
        c = canon_with(
            packages=("acme",),
            agents=[("acme", ["beta"])],
            federation_=federation(
                modules_={"it": {}},
                teams={"acme": {"modules": {"it": {"require_qa": ["promoted"]}}}},
                projects={"acme": {"modules": {"it": {"require_qa": ["merged",
                                                                     "promoted"]}}}}))
        entries = {row["project"]: row for row in modules.declarations(c.federation, c)}
        self.assertEqual(entries["acme"]["scope"], "project")
        self.assertEqual(entries["acme"]["rules"][-1]["states"],
                         ["merged", "promoted"])
        self.assertEqual(entries["beta"]["scope"], "team")
        self.assertEqual(entries["beta"]["rules"][-1]["states"], ["promoted"])

    def test_a_scope_can_add_a_module_without_losing_the_order(self):
        fake = sample()
        with external_points(FakeEntryPoint(fake)):
            c = canon_with(federation_=federation(
                modules_=["it"],
                projects={"acme": {"modules": ["sample"]}}))
            entries = modules.declarations(c.federation, c)
        self.assertEqual(rows_of(entries), [("acme", "it"), ("acme", "sample")])
        self.assertEqual([row["scope"] for row in entries], ["federation", "project"])

    def test_a_team_declaration_needs_the_canon_to_name_its_projects(self):
        with self.assertRaisesRegex(ValueError, "needs the canon"):
            modules.declarations(federation(teams={"acme": {"modules": ["it"]}}))

    def test_malformed_team_and_scope_selections_are_refused(self):
        cases = (
            federation(modules_="it"),
            federation(teams=[]),
            federation(teams={"acme": []}),
            federation(teams={"acme": {"modules": "it"}}),
            federation(teams={"acme": {"modules": ["it", "it"]}}),
        )
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                modules.declarations(raw)

    def test_dev_stays_unselectable_at_every_scope(self):
        """Decision 2: dev is integrated and always active, never selected."""
        for raw in (federation(modules_=["dev"]),
                    federation(teams={"acme": {"modules": ["dev"]}})):
            with self.subTest(raw=raw), self.assertRaisesRegex(
                    ValueError, "types must be unique non-empty"):
                modules.declarations(raw, canon_with())


class RefusedEntryPointTest(unittest.TestCase):
    def test_a_refused_entry_point_that_no_scope_activates_is_a_warning(self):
        with external_points(FakeEntryPoint(ImportError("boom"), name="broken")):
            resolution = modules.resolve(federation(projects={"acme": {"modules": []}}),
                                         canon_with())
        self.assertEqual(resolution.rows, ())
        self.assertEqual([warning["name"] for warning in resolution.warnings],
                         ["broken"])
        self.assertEqual(resolution.warnings[0]["code"], "module-refused")

    def test_an_activated_refused_entry_point_still_blocks_the_canon(self):
        with external_points(FakeEntryPoint(ImportError("boom"), name="broken")):
            with self.assertRaisesRegex(ValueError, "activated but refused"):
                modules.declarations(
                    federation(projects={"acme": {"modules": ["broken"]}}),
                    canon_with())

    def test_canon_check_reports_a_refused_inactive_entry_point_as_a_warning(self):
        c = canon.Canon(root=".", readable=True,
                        federation=federation(projects={"acme": {"modules": []}}))
        with external_points(FakeEntryPoint(ImportError("boom"), name="broken")):
            findings = canon.validate(c)
        warning = next(finding for finding in findings if finding.code == "module-refused")
        self.assertEqual(warning.severity, canon.WARNING)
        self.assertIn("not activated", warning.message)
        self.assertFalse(any(finding.code == "project-modules-invalid"
                             for finding in findings))

    def test_canon_check_keeps_an_activated_refusal_as_an_error(self):
        c = canon.Canon(root=".", readable=True,
                        federation=federation(
                            projects={"acme": {"modules": ["broken"]}}))
        with external_points(FakeEntryPoint(ImportError("boom"), name="broken")):
            findings = canon.validate(c)
        error = next(finding for finding in findings
                     if finding.code == "project-modules-invalid")
        self.assertEqual(error.severity, canon.ERROR)
        self.assertIn("activated but refused", error.message)


class ActiveViewTest(unittest.TestCase):
    def setUp(self):
        discover = external_points()
        discover.start()
        self.addCleanup(discover.stop)

    def test_without_a_database_the_active_rows_come_from_the_local_canon(self):
        c = canon_with(packages=("acme",),
                       federation_=federation(modules_=["it"]))
        payload = modules.active(None, [c])
        self.assertEqual([(row["canon"], row["scope"], row["project"],
                           row["module"]) for row in payload["active"]],
                         [("test", "federation", "acme", "it")])
        row = payload["active"][0]
        self.assertEqual(row["version"], registry.discover().get("it").version)
        self.assertEqual(len(row["descriptor_sha256"]), 64)

    def test_modules_active_cli_falls_back_to_the_canon_without_a_database(self):
        c = canon_with(packages=("acme",),
                       federation_=federation(modules_=["it"]))
        out = io.StringIO()
        with mock.patch("ameesh.config.load", return_value=object()), \
                mock.patch("ameesh.canon.load_configured", return_value=[c]), \
                mock.patch("ameesh.db.connect",
                           side_effect=db_mod.Unavailable("offline")), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = registry.main(["--active", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual([(row["canon"], row["project"], row["module"])
                          for row in payload["active"]],
                         [("test", "acme", "it")])

    def test_modules_active_cli_writes_warnings_and_refusals_to_stderr(self):
        # #4407 (auditeur): both stderr paths of `--active` print without a
        # NameError — a refused, inactive entry point, and no canon nor database.
        warned = {"active": [], "refused": [], "warnings": [
            {"code": "module-refused", "name": "bad",
             "message": "installed module 'bad' is refused and not activated: x"}]}
        for payload, have, code_expected, needle in (
                (warned, (True, True), 0, "warning: installed module 'bad'"),
                ({"active": [], "refused": [], "warnings": []}, (False, False), 1,
                 "no canon and no database")):
            err = io.StringIO()
            with mock.patch.object(registry, "_active_payload",
                                   return_value=(payload,) + have), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err):
                code = registry.main(["--active"])
            self.assertEqual(code, code_expected)
            self.assertIn(needle, err.getvalue())


class ActivationStorageTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM project_modules")

    def test_sync_stores_the_resolved_rows_and_active_reads_them(self):
        c = canon_with(packages=("acme", "beta"),
                       agents=[("acme", ["acme", "nexlink"])],
                       federation_=federation(
                           modules_=["it"],
                           teams={"acme": {"modules": {"it": {"require_qa": ["promoted"]}}}}))
        modules.sync(self.db, c)
        stored = ModuleStore(self.db).stored()
        self.assertEqual(sorted(row["project"] for row in stored),
                         ["acme", "beta", "nexlink"])
        self.assertTrue(all(row["canon"] == "" for row in stored))
        payload = modules.active(self.db, [c])
        self.assertEqual([(row["project"], row["scope"], row["team"])
                          for row in payload["active"]],
                         [("acme", "team", "acme"), ("beta", "federation", None),
                          ("nexlink", "team", "acme")])
        self.assertTrue(all(len(row["descriptor_sha256"]) == 64
                            for row in payload["active"]))

    def test_doctor_is_clean_when_the_applied_rows_match_the_canon(self):
        c = canon_with(federation_=federation(modules_=["it"]))
        modules.sync(self.db, c)
        ok, lines = modules.doctor_lines(self.db, [c])
        self.assertTrue(ok, lines)
        self.assertIn("no problem", "\n".join(lines))

    def test_doctor_reports_an_active_module_that_is_not_installed(self):
        fake = sample()
        c = canon_with(federation_=federation(modules_=["it", "sample"]))
        with external_points(FakeEntryPoint(fake)):
            modules.sync(self.db, c)
        ok, lines = modules.doctor_lines(self.db, [])
        self.assertFalse(ok)
        self.assertIn("active module sample is not installed", "\n".join(lines))

    def test_doctor_warns_when_the_installed_descriptor_moved(self):
        fake = sample()

        def moved(options=None):
            return {"types": ["docs"], "schemas": {"changed": {}}, "rules": []}

        c = canon_with(federation_=federation(modules_=["it", "sample"]))
        with external_points(FakeEntryPoint(fake)):
            modules.sync(self.db, c)
            fake.descriptor = moved
            ok, lines = modules.doctor_lines(self.db, [c])
        text = "\n".join(lines)
        self.assertTrue(ok, text)
        self.assertIn("installed descriptor of sample differs", text)
        self.assertIn("canon sync to redo", text)

    def test_doctor_warns_when_a_declared_module_has_not_been_applied(self):
        c = canon_with(federation_=federation(modules_=["it"]))
        ok, lines = modules.doctor_lines(self.db, [c])
        text = "\n".join(lines)
        self.assertTrue(ok, text)
        self.assertIn("module it is declared but not applied", text)

    def test_doctor_reports_a_refused_inactive_entry_point_as_a_warning(self):
        c = canon_with(federation_=federation(modules_=[]))
        with external_points(FakeEntryPoint(ImportError("boom"), name="broken")):
            ok, lines = modules.doctor_lines(self.db, [c])
        text = "\n".join(lines)
        self.assertTrue(ok, text)
        self.assertIn("module broken refused and not activated", text)

    def test_doctor_fails_on_an_activated_refused_module(self):
        c = canon_with(federation_=federation(modules_=["broken"]))
        with external_points(FakeEntryPoint(ImportError("boom"), name="broken")):
            ok, lines = modules.doctor_lines(self.db, [c])
        self.assertFalse(ok)
        self.assertIn("activated but refused", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
