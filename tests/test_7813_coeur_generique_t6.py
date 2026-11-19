# SPDX-License-Identifier: AGPL-3.0-only
"""#7813 (#7637 tranche 6): the last Nexlink names leave the generic core.

The core reads the generic schemas and names; the historical Nexlink page and
dispatch-plan names are aliases declared by the module, its application and its
repository planner stay unchanged. The decision 0026 reserved names are
declared by the module too, never compiled by the core. The bare profile has
neither the aliases, the reserved names, nor a compiled project name.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from tests import support  # noqa: F401 - isolate the host configuration first
from ameesh import (ci, cost, fingerprints, release, release_cutoff,
                    role_timers, server_faults)
from ameesh.approve import config as approve_config
from ameesh.modules import registry

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "fingerprints", "nexlink-page.json")


def page():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


class ModuleAliasTest(unittest.TestCase):
    def test_the_module_declares_the_historical_names(self):
        with mock.patch.dict(os.environ, {"AMEESH_MODULES": "default"}):
            self.assertEqual(registry.fingerprint_schemas(), ("nexlink-fingerprints/1",))
            self.assertEqual(registry.dispatch_plan_schemas(),
                             ("nexlink-dispatch-due-parts/1",))

    def test_the_bare_profile_declares_no_alias(self):
        with mock.patch.dict(os.environ, {"AMEESH_MODULES": "bare"}):
            self.assertEqual(registry.fingerprint_schemas(), ())
            self.assertEqual(registry.dispatch_plan_schemas(), ())


class FingerprintPageSchemaTest(unittest.TestCase):
    def journal_page(self):
        folder = tempfile.mkdtemp(prefix="7813-fingerprints-")
        journal = server_faults.Journal(os.path.join(folder, "faults.json"),
                                        "ameesh-approve")
        return journal.page()

    def test_the_generic_schema_and_the_historical_alias_are_both_read(self):
        with mock.patch.dict(os.environ, {"AMEESH_MODULES": "default"}):
            for schema in (fingerprints.SCHEMA, "nexlink-fingerprints/1"):
                with self.subTest(schema=schema):
                    self.assertEqual(
                        len(fingerprints.parse_page(dict(page(), schema=schema))[0]), 2)

    def test_another_schema_is_refused(self):
        with self.assertRaisesRegex(fingerprints.FingerprintError, "schema"):
            fingerprints.parse_page(dict(page(), schema="other/1"))

    def test_the_bare_profile_reads_only_the_generic_schema(self):
        with mock.patch.dict(os.environ, {"AMEESH_MODULES": "bare"}):
            fingerprints.parse_page(dict(page(), schema=fingerprints.SCHEMA))
            with self.assertRaisesRegex(fingerprints.FingerprintError, "schema"):
                fingerprints.parse_page(dict(page(), schema="nexlink-fingerprints/1"))

    def test_the_served_page_uses_the_generic_schema(self):
        served = self.journal_page()
        self.assertEqual(served["schema"], "ameesh-fingerprints/1")
        self.assertEqual(fingerprints.parse_page(served), ([], None))


class DispatchPlanSchemaTest(unittest.TestCase):
    BRANCH = "lot/7813"
    SHA = "a" * 40

    def report(self, schema):
        return {"schema": schema, "ref": self.BRANCH, "head": self.SHA,
                "dryRun": True, "failures": [],
                "parts": [{"workflow": "suite.yml"}],
                "argv": [{"argv": ["workflow", "run", "suite.yml", "--ref", self.BRANCH,
                                   "-f", "expected_sha=" + self.SHA]}]}

    def plan(self, schema):
        api = lambda path: {"content": "whatever"}
        with mock.patch("ameesh.prevalidation_calls.repository", return_value="/clone"), \
             mock.patch("ameesh.release_cutoff._git"), \
             mock.patch("ameesh.release_cutoff._drop_worktree"), \
             mock.patch("tempfile.mkdtemp", return_value="/folder"), \
             mock.patch("subprocess.run", return_value=types.SimpleNamespace(
                 returncode=0, stdout=json.dumps(self.report(schema)), stderr="")):
            return ci._repository_dispatch_plan(
                None, None, {"id": 1}, api, "example/product", self.BRANCH, self.SHA)

    def test_the_generic_plan_schema_is_accepted(self):
        self.assertEqual(self.plan(ci.DISPATCH_PLAN_SCHEMA),
                         {"suite.yml": {"expected_sha": self.SHA}})

    def test_the_module_alias_keeps_the_nexlink_planner_readable(self):
        with mock.patch.dict(os.environ, {"AMEESH_MODULES": "default"}):
            self.assertEqual(self.plan("nexlink-dispatch-due-parts/1"),
                             {"suite.yml": {"expected_sha": self.SHA}})

    def test_the_bare_profile_refuses_the_historical_alias(self):
        with mock.patch.dict(os.environ, {"AMEESH_MODULES": "bare"}):
            with self.assertRaisesRegex(ci.GelError, "invalid dispatch plan"):
                self.plan("nexlink-dispatch-due-parts/1")


class ReleaseDefaultProjectTest(unittest.TestCase):
    BASE = {"repo": ".", "target": "main"}

    def setUp(self):
        patcher = mock.patch.object(release_cutoff.radar, "settings_of",
                                    return_value=types.SimpleNamespace(projects={}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def cfg(self, release_block):
        return types.SimpleNamespace(host="host", release=release_block,
                                     humans="", radar={})

    def test_a_sole_project_is_not_inferred(self):
        with self.assertRaisesRegex(release_cutoff.ReleaseError, "--project is required"):
            release_cutoff.default_project(self.cfg(
                {"host": "host", "projects": {"only": self.BASE}}))

    def test_the_configured_default_wins(self):
        self.assertEqual(release_cutoff.default_project(self.cfg(
            {"host": "host", "default_project": "b",
             "projects": {"a": self.BASE, "b": self.BASE}})), "b")

    def test_several_projects_without_a_default_require_the_option(self):
        with self.assertRaisesRegex(release_cutoff.ReleaseError, "--project is required"):
            release_cutoff.default_project(self.cfg(
                {"host": "host", "projects": {"a": self.BASE, "b": self.BASE}}))

    def test_a_default_outside_release_projects_is_refused(self):
        with self.assertRaisesRegex(release_cutoff.ReleaseError, "not declared"):
            release_cutoff.default_project(self.cfg(
                {"host": "host", "default_project": "z",
                 "projects": {"a": self.BASE}}))

    def test_the_handler_reads_the_configuration(self):
        cfg = self.cfg({"host": "host", "projects": {"only": self.BASE}})
        with self.assertRaisesRegex(release_cutoff.ReleaseError, "--project is required"):
            release._project_name(cfg, types.SimpleNamespace(project=None))
        self.assertEqual(release._project_name(cfg, types.SimpleNamespace(project="x")),
                         "x")
        cfg = self.cfg({"host": "host", "default_project": "only",
                        "projects": {"only": self.BASE}})
        self.assertEqual(release._project_name(cfg, types.SimpleNamespace(project=None)),
                         "only")

    def test_attest_and_abandon_take_no_compiled_project(self):
        parser = argparse.ArgumentParser()
        release.add_parser(parser.add_subparsers(dest="release_command"),
                           lambda *args: 0)
        self.assertIsNone(
            parser.parse_args(["release", "attest", "--version", "1"]).project)
        self.assertIsNone(
            parser.parse_args(["release", "abandon", "--reason", "red"]).project)


class ApproveReservedNamesTest(unittest.TestCase):
    MANATY = dict(reserved_zones=("nexlink.ph",),
                  reserved_hosts=("ameesh.nexlink.ph",))

    def config(self, rp_id, **values):
        values.setdefault("origins", ("https://" + rp_id,))
        return approve_config.ApproveConfig(rp_id=rp_id, **values).validate()

    def test_a_declared_zone_stays_reserved(self):
        for rp_id, fragment in (("nexlink.ph", "reserved zone"),
                                ("ameesh.nexlink.ph", "reserved host")):
            with self.subTest(rp_id=rp_id):
                with self.assertRaisesRegex(approve_config.ApproveConfigError, fragment):
                    self.config(rp_id, **self.MANATY)

    def test_a_team_host_under_the_zone_is_accepted(self):
        self.assertEqual(self.config("equipe-a.nexlink.ph", **self.MANATY).rp_id,
                         "equipe-a.nexlink.ph")

    def test_the_module_keeps_the_decision_0026_names_reserved(self):
        with mock.patch.dict(os.environ, {"AMEESH_MODULES": "default"}):
            self.assertEqual(registry.reserved_zones(), ("nexlink.ph",))
            self.assertEqual(registry.reserved_hosts(), ("ameesh.nexlink.ph",))
            for rp_id, fragment in (("nexlink.ph", "reserved zone"),
                                    ("ameesh.nexlink.ph", "reserved host")):
                with self.subTest(rp_id=rp_id):
                    with self.assertRaisesRegex(approve_config.ApproveConfigError, fragment):
                        self.config(rp_id)

    def test_a_config_file_without_reserved_names_still_refuses_the_apex(self):
        with tempfile.TemporaryDirectory(prefix="7813-approve-") as folder:
            path = os.path.join(folder, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"rp_id": "nexlink.ph",
                           "origins": ["https://nexlink.ph"]}, fh)
            with mock.patch.dict(os.environ, {"AMEESH_MODULES": "default"}):
                with self.assertRaisesRegex(approve_config.ApproveConfigError, "reserved"):
                    approve_config.load({}, path=path)

    def test_the_bare_profile_compiles_no_reserved_name(self):
        with mock.patch.dict(os.environ, {"AMEESH_MODULES": "bare"}):
            self.assertFalse(hasattr(approve_config, "BUILTIN_RESERVED_ZONES"))
            self.assertFalse(hasattr(approve_config, "BUILTIN_RESERVED_HOSTS"))
            self.assertEqual(self.config("nexlink.ph").rp_id, "nexlink.ph")


class PricesPathTest(unittest.TestCase):
    def test_the_default_price_file_is_under_ameesh(self):
        with tempfile.TemporaryDirectory(prefix="7813-prices-") as folder:
            with mock.patch.dict(os.environ, {"AMEESH_PRICES": ""}):
                book = cost.CostBook(state_dir=folder)
        self.assertEqual(book.prices_path,
                         os.path.expanduser("~/.config/ameesh/prices.json"))
        self.assertNotIn("nexlink", book.prices_path)


class RemotePushKindsTest(unittest.TestCase):
    def reach(self):
        notify = {"routes": {"human:owner": ["desktop"]}}
        return role_timers.notify_reach(
            types.SimpleNamespace(notify=notify), responsible="human:owner", env={})

    def test_the_gap_lists_the_registered_remote_push_kinds(self):
        with mock.patch.dict(os.environ, {"AMEESH_MODULES": "default"}):
            _humans, error = self.reach()
        self.assertIn("(ntfy, slack, nexlink)", error)

    def test_the_bare_profile_lists_only_the_core_kinds(self):
        with mock.patch.dict(os.environ, {"AMEESH_MODULES": "bare"}):
            _humans, error = self.reach()
        self.assertIn("(ntfy, slack)", error)
        self.assertNotIn("nexlink", error)


if __name__ == "__main__":
    unittest.main()
