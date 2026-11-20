# SPDX-License-Identifier: AGPL-3.0-only
"""#7860 (manaty/ameesh#6086): a module a team or a project selects and this
host does not have no longer invalidates the whole canon.

`canon check` reports it as the `project-module-missing` warning; `canon sync`
refuses the placement of the agents whose projects select it (stable head
`module missing on this host`, reason `module_missing` in the agent
observation), the other agents stay claimable, and the applied rows of the
missing pair are kept for the hosts that have the module. A module the whole
canon selects (federation scope) and an activated refused entry point still
refuse the canon.
"""
from __future__ import annotations

import os
from unittest import mock
import types as types_mod
import unittest

from tests import support  # noqa: F401  isolate host config before product imports
from ameesh import agent_observation, canon, canon_cli, canon_sync, modules, placement, registry
from ameesh.modules.storage import ModuleStore

from . import test_canon
from .test_canon import write
from .test_l98_modules_activation import (FakeEntryPoint, canon_with, external_points,
                                          federation, rows_of, sample)

TPK = federation(projects={"thepackengers": {"modules": ["it", "tpk"]},
                           "acme": {"modules": ["it"]}})


def agent(*projects):
    return types_mod.SimpleNamespace(effective_projects=list(projects))


class ResolveTest(unittest.TestCase):
    def setUp(self):
        discover = external_points()
        discover.start()
        self.addCleanup(discover.stop)

    def test_a_missing_project_module_is_a_structured_warning(self):
        resolution = modules.resolve(TPK, canon_with(packages=("acme", "thepackengers")))
        # the present modules still compile, for the project concerned too
        self.assertEqual(rows_of(resolution.rows),
                         [("acme", "it"), ("thepackengers", "it")])
        [warning] = resolution.warnings
        self.assertEqual(
            {key: warning[key] for key in ("code", "name", "scope", "project", "projects")},
            {"code": "project-module-missing", "name": "tpk", "scope": "project",
             "project": "thepackengers", "projects": ["thepackengers"]})
        self.assertIn("ameesh.modules", warning["install"])
        self.assertIn("ameesh canon sync", warning["install"])

    def test_a_missing_team_module_names_the_team_projects(self):
        c = canon_with(packages=("tpk",), agents=[("tpk", ["tpk", "thepackengers"])],
                       federation_=federation(teams={"tpk": {"modules": ["tpk"]}}))
        [warning] = modules.resolve(c.federation, c).warnings
        self.assertEqual((warning["scope"], warning["team"], warning["projects"]),
                         ("team", "tpk", ["thepackengers", "tpk"]))

    def test_a_missing_federation_module_still_refuses_the_canon(self):
        with self.assertRaisesRegex(ValueError, "unknown installed module: tpk"):
            modules.resolve(federation(modules_=["tpk"]), canon_with())

    def test_an_activated_refused_entry_point_still_refuses_the_canon(self):
        with external_points(FakeEntryPoint(ImportError("boom"), name="tpk")):
            with self.assertRaisesRegex(ValueError, "activated but refused"):
                modules.resolve(TPK, canon_with())

    def test_only_the_agents_of_the_projects_concerned_are_refused(self):
        absent = list(modules.resolve(TPK, canon_with()).warnings)
        self.assertEqual(modules.agent_needs(absent, ["acme"]), [])
        self.assertEqual(modules.agent_needs(absent, ["acme", "thepackengers"]),
                         [("tpk", "thepackengers")])
        admitted = placement.Verdict("other", "mac", True, "", profile="p")
        modules.refuse_missing(admitted, agent("acme"), absent)
        self.assertEqual((admitted.ok, admitted.diagnostic), (True, ""))
        refused = placement.Verdict("ellie", "mac", True, "", profile="p")
        modules.refuse_missing(refused, agent("thepackengers"), absent)
        self.assertIs(refused.ok, False)
        self.assertEqual(refused.diagnostic, "module missing on this host: "
                                             "tpk (project thepackengers)")


class CanonCheckTest(unittest.TestCase):
    def setUp(self):
        discover = external_points()
        discover.start()
        self.addCleanup(discover.stop)

    def test_canon_check_warns_without_any_blocking_error(self):
        c = canon.Canon(root=".", readable=True, federation=TPK)
        findings = canon.validate(c)
        warning = next(f for f in findings if f.code == "project-module-missing")
        self.assertEqual(warning.severity, canon.WARNING)
        self.assertIn("tpk", warning.message)
        self.assertNotIn("project-modules-invalid", {f.code for f in findings})
        self.assertEqual(canon.blocking(findings).global_errors, [])

    def test_canon_check_keeps_a_missing_federation_module_as_an_error(self):
        c = canon.Canon(root=".", readable=True, federation=federation(modules_=["tpk"]))
        error = next(f for f in canon.validate(c) if f.code == "project-modules-invalid")
        self.assertEqual(error.severity, canon.ERROR)


class ObservationTest(unittest.TestCase):
    ROW = {"mode": "execute", "retired": False, "ephemeral_expired": False,
           "host_claims_open": True, "handoff_fenced": False, "releve": "hote",
           "responsible": "human:x", "governed": True, "canon_status": "ok",
           "placement_ok": False, "placement_profile": "p",
           "placement_profile_current": "p", "claim_predicate": False}

    def reasons(self, diagnostic):
        row = dict(self.ROW, placement_diagnostic=diagnostic)
        return agent_observation._admission(row, responsible_required=False, fresh=True,
                                            ref_matches=True)["reasons"]

    def test_a_missing_module_has_its_own_stable_reason(self):
        self.assertIn("module_missing", agent_observation.REASONS)
        self.assertEqual(self.reasons("module missing on this host: tpk (project t)"),
                         ["module_missing"])
        self.assertEqual(self.reasons("module missing on this host: tpk (project t); "
                                      "harness codex not allowed"),
                         ["module_missing", "placement_refused"])
        self.assertEqual(self.reasons("harness codex not allowed"), ["placement_refused"])


class CheckHostTest(unittest.TestCase):
    """#7868 (manaty/ameesh#6101): module availability is a local fact."""

    def check(self, local_host, requested_host):
        c = types_mod.SimpleNamespace(
            readable=True, root="/canon", untrusted=False, id="", is_default=True,
            sources=[], agent=lambda name: agent("acme"))
        verdicts = [placement.Verdict("avatar", host, True)
                    for host in ("local", "remote")]
        absent = [{"code": modules.MISSING, "name": "extra", "projects": ["acme"]}]
        sink = []
        with mock.patch.object(canon_cli, "_recorded", return_value=None), \
                mock.patch.object(canon_cli, "_process_block_json", return_value=None), \
                mock.patch.object(canon, "validate", return_value=[]), \
                mock.patch.object(placement, "verdicts", return_value=verdicts), \
                mock.patch.object(modules, "missing", return_value=absent), \
                mock.patch("ameesh.persona_memory.canon_findings", return_value=[]), \
                mock.patch("ameesh.canon_review_coverage.findings", return_value=[]), \
                mock.patch.object(canon_sync, "assess", return_value=("ok", "")):
            code = canon_cli._check_one(
                types_mod.SimpleNamespace(host=local_host),
                types_mod.SimpleNamespace(host=requested_host, json=True), c, sink=sink)
        self.assertEqual(code, 0)
        return {v["host"]: v for v in sink[0]["placements"]}

    def test_local_default_and_explicit_host_refuse_only_local_placement(self):
        for requested in (None, "local"):
            with self.subTest(host=requested):
                rows = self.check("local", requested)
                self.assertFalse(rows["local"]["placement_ok"])
                self.assertIn("module missing on this host", rows["local"]["placement_diagnostic"])
                self.assertTrue(rows["remote"]["placement_ok"])

    def test_remote_host_does_not_inherit_local_missing_modules(self):
        rows = self.check("local", "remote")
        self.assertTrue(rows["local"]["placement_ok"])
        self.assertTrue(rows["remote"]["placement_ok"])
        self.assertEqual(rows["remote"]["placement_diagnostic"], "")

    def test_without_configured_host_explicit_host_is_the_only_known_host(self):
        rows = self.check("", "remote")
        self.assertTrue(rows["local"]["placement_ok"])
        self.assertFalse(rows["remote"]["placement_ok"])


class SyncTest(test_canon._CanonDbCase):
    """The reduced canon of the ticket: `tpk` absent, selected by one project."""

    def setUp(self):
        super().setUp()
        discover = external_points()
        discover.start()
        self.addCleanup(discover.stop)
        path = os.path.join(self.root, "agents", "relecteur.md")
        with open(path, encoding="utf-8") as source:
            card = source.read()
        write(self.root, "agents/relecteur.md",
              card.replace("team: acme-web", "team: thepackengers"))
        write(self.root, "federation.yaml",
              'federation: "0.1"\nmembers: []\nextensions:\n  ameesh:\n    projects:\n'
              "      thepackengers:\n        modules: [tpk]\n")

    def test_the_canon_stays_ok_and_only_the_agent_concerned_is_refused(self):
        report = self.sync()
        self.assertEqual(report.status, canon_sync.CANON_OK)
        self.assertEqual(canon_sync.state(self.db, "atelier")["status"], "ok")
        claimable = sorted(row["name"] for row in registry.claimable(
            self.db, "atelier", require_responsible=False))
        self.assertEqual(claimable, ["orchestre"])
        row = self.db.query("SELECT placement_ok, placement_diagnostic FROM agent_registry "
                            "WHERE name = 'relecteur'")[0]
        self.assertIs(row["placement_ok"], False)
        self.assertTrue(row["placement_diagnostic"].startswith(
            "module missing on this host: tpk (project thepackengers)"),
            row["placement_diagnostic"])


class StorageTest(test_canon._CanonDbCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM project_modules")

    def test_a_host_without_the_module_keeps_the_applied_rows(self):
        c = canon_with(packages=("acme", "thepackengers"), federation_=TPK)
        with external_points(FakeEntryPoint(sample(name="tpk"), name="tpk")):
            modules.sync(self.db, c)
        with external_points():
            modules.sync(self.db, c)
            ok, lines = modules.doctor_lines(self.db, [c])
        self.assertIn(("thepackengers", "tpk"),
                      [(row["project"], row["module"])
                       for row in ModuleStore(self.db).stored()])
        text = "\n".join(lines)
        self.assertTrue(ok, text)
        self.assertIn("module tpk (project thepackengers) is not installed on this host",
                      text)
        self.assertIn("pip install", text)
        self.assertNotIn("KO", text)
        # a declaration removed from the canon is no longer kept
        gone = canon_with(packages=("acme", "thepackengers"), federation_=federation(
            projects={"thepackengers": {"modules": ["it"]}, "acme": {"modules": ["it"]}}))
        with external_points():
            modules.sync(self.db, gone)
        self.assertNotIn("tpk", [row["module"] for row in ModuleStore(self.db).stored()])


if __name__ == "__main__":
    unittest.main()
