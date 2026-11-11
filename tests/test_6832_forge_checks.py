# SPDX-License-Identifier: AGPL-3.0-only
"""#6832 : la déclaration des checks requis survit à une relève d'hôte.

Le 11/11, le pilote a échoué d'AG2 vers AG1 et `ameesh work merge-pr` a refusé
Nexlink : la protection de `develop` répond 403 de **plan** GitHub (dépôt privé
hors Pro) et la déclaration `radar.projects.nexlink.required_checks` vide
n'existait que dans la configuration d'AG2 (#5152, #211196). La déclaration est
désormais portée par le paquet (`forge_checks.json`, données de paquet), donc
versionnée et installée sur chaque hôte par le chemin de staging.

Témoins : 403 de plan + déclaration versionnée vide → la garde passe (aucun
check exigé par la forge) ; sans déclaration → refus inchangé ; 403 de droits
→ refus inchangé ; un hôte candidat d'un rôle élu sans déclaration pour un
projet dont la forge prouve le 403 de plan → doctor KO qui nomme le projet et
la cible ; déclaration présente ou protection lisible → doctor vert.
"""
from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from unittest import mock

from ameesh import forge_checks, fire, land, role_standings_pg
from ameesh.modules.dev import merge_pr

PLAN = ("gh: Upgrade to GitHub Pro or make this repository public to enable this "
        "feature. (HTTP 403)")
RIGHTS = "gh: Resource not accessible by integration (HTTP 403)"


class DetailGh:
    """Un `gh` dont toutes les lectures API rendent le même détail."""

    def __init__(self, detail: str):
        self.detail = detail
        self.calls: list = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return types.SimpleNamespace(returncode=1, stdout="", stderr=self.detail)


class ReadableGh:
    """Une protection lisible et des rulesets vides : aucune déclaration requise."""

    def __init__(self):
        self.calls: list = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        path = argv[1] if len(argv) > 1 else ""
        if path.endswith("/protection"):
            body = {"required_status_checks": {"contexts": ["tests"], "checks": []}}
        else:
            body = []
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")


class NexlinkPlanGh(ReadableGh):
    """Nexlink prouve le 403 de plan, ameesh se lit normalement."""

    def __call__(self, argv, **kwargs):
        path = argv[1] if len(argv) > 1 else ""
        if "manaty/nexlink" in path:
            self.calls.append(list(argv))
            return types.SimpleNamespace(returncode=1, stdout="", stderr=PLAN)
        return super().__call__(argv, **kwargs)


def _inventory(projects, path: str) -> str:
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({"schema": forge_checks.SCHEMA, "projects": projects}, stream)
    return path


class InventoryTest(unittest.TestCase):
    """Le fichier livré et sa lecture stricte."""

    def test_the_package_declares_nexlink_empty(self):
        # `()` est une déclaration, pas une absence : elle couvre le 403 de plan.
        self.assertEqual(forge_checks.declared("manaty/nexlink"), ())
        self.assertEqual(forge_checks.declared("https://github.com/manaty/nexlink.git"), ())
        self.assertIsNone(forge_checks.declared("manaty/ameesh"))
        self.assertIsNone(forge_checks.declared(""))

    def test_a_missing_inventory_is_no_declaration(self):
        missing = os.path.join(tempfile.gettempdir(), "ameesh-forge-checks-absent.json")
        try:
            os.unlink(missing)
        except FileNotFoundError:
            pass
        self.assertIsNone(forge_checks.inventory(missing))
        self.assertIsNone(forge_checks.declared("manaty/nexlink", path=missing))

    def test_an_invalid_inventory_is_never_ignored(self):
        cases = {
            "unknown top-level key": {"schema": forge_checks.SCHEMA, "projects": {},
                                      "extra": 1},
            "bad schema": {"schema": "ameesh-forge-checks/2", "projects": {}},
            "missing projects": {"schema": forge_checks.SCHEMA},
            "unknown project key": {"schema": forge_checks.SCHEMA, "projects": {
                "p": {"github": "o/r", "required_checks": [], "extra": 1}}},
            "bad repository": {"schema": forge_checks.SCHEMA, "projects": {
                "p": {"github": "not-a-repo", "required_checks": []}}},
            "checks not a list": {"schema": forge_checks.SCHEMA, "projects": {
                "p": {"github": "o/r", "required_checks": "tests"}}},
            "empty check name": {"schema": forge_checks.SCHEMA, "projects": {
                "p": {"github": "o/r", "required_checks": [" "]}}},
            "duplicate check name": {"schema": forge_checks.SCHEMA, "projects": {
                "p": {"github": "o/r", "required_checks": ["tests", "tests"]}}},
            "duplicate repository": {"schema": forge_checks.SCHEMA, "projects": {
                "p": {"github": "o/r", "required_checks": []},
                "q": {"github": "O/R", "required_checks": []}}},
            "why not text": {"schema": forge_checks.SCHEMA, "projects": {
                "p": {"github": "o/r", "required_checks": [], "why": 1}}},
        }
        for label, raw in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "forge_checks.json")
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump(raw, stream)
                with self.assertRaises(forge_checks.InventoryError):
                    forge_checks.inventory(path)

    def test_a_duplicated_json_key_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "forge_checks.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write('{"schema": "%s", "schema": "%s", "projects": {}}'
                             % (forge_checks.SCHEMA, forge_checks.SCHEMA))
            with self.assertRaises(forge_checks.InventoryError):
                forge_checks.inventory(path)


class RequiredChecksTest(unittest.TestCase):
    """La garde de fusion : la déclaration versionnée s'ajoute, jamais ne retire."""

    def test_a_versioned_empty_declaration_covers_a_plan_403(self):
        self.assertEqual(
            fire.required_checks("manaty/nexlink", "develop", None, gh=DetailGh(PLAN),
                                 declared=forge_checks.declared("manaty/nexlink")), ())

    def test_without_a_declaration_the_plan_403_still_refuses(self):
        with self.assertRaises(land.LandError):
            fire.required_checks("manaty/nexlink", "develop", None, gh=DetailGh(PLAN))

    def test_a_rights_403_is_not_covered_by_an_empty_declaration(self):
        with self.assertRaises(land.LandError):
            fire.required_checks("manaty/nexlink", "develop", None, gh=DetailGh(RIGHTS),
                                 declared=forge_checks.declared("manaty/nexlink"))

    def test_a_versioned_declaration_adds_its_names_to_a_readable_protection(self):
        self.assertEqual(
            fire.required_checks("manaty/ameesh", "main", None, gh=ReadableGh(),
                                 declared=("coverage",)),
            (("tests", None), ("coverage", None)))

    def test_an_absent_versioned_declaration_keeps_the_project_declaration(self):
        project = types.SimpleNamespace(required_checks=("coverage",),
                                        required_checks_declared=True)
        self.assertEqual(
            fire.required_checks("manaty/ameesh", "main", project, gh=ReadableGh(),
                                 declared=None),
            (("tests", None), ("coverage", None)))

    def test_an_unreadable_inventory_refuses_the_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "forge_checks.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{not json")
            with mock.patch.object(forge_checks, "PATH", path):
                with self.assertRaises(land.LandError):
                    fire.versioned_declaration("manaty/nexlink")


class MergePrGuardTest(unittest.TestCase):
    """Le geste du constat : `work merge-pr` sur Nexlink, sur l'hôte sans radar."""

    def test_the_plan_403_passes_with_the_versioned_declaration(self):
        self.assertEqual(
            merge_pr._required_refusal("manaty/nexlink", "develop", None, [],
                                       gh=DetailGh(PLAN)), "")

    def test_a_rights_403_still_refuses(self):
        refusal = merge_pr._required_refusal("manaty/nexlink", "develop", None, [],
                                             gh=DetailGh(RIGHTS))
        self.assertIn("cannot be proven", refusal)
        self.assertIn("Resource not accessible", refusal)

    def test_an_undeclared_plan_403_still_refuses(self):
        refusal = merge_pr._required_refusal("manaty/other", "main", None, [],
                                             gh=DetailGh(PLAN))
        self.assertIn("cannot be proven", refusal)


def _cfg(**radar_projects):
    return types.SimpleNamespace(
        host="ameesh-agents-1",
        github={"projects": {"nexlink": {"repo": "manaty/nexlink",
                                          "base": ["develop"]},
                             "ameesh": "manaty/ameesh"}},
        release={},
        radar={"projects": radar_projects})


class DoctorTest(unittest.TestCase):
    """Le doctor : rouge sur un hôte candidat sans déclaration, pas ailleurs."""

    def _doctor(self, gh, *, cfg=None, path=None, roles=(("orchestrateur",),)):
        rows = [{"name": name} for (name,) in roles]
        with mock.patch.object(role_standings_pg, "elected_roles", return_value=rows):
            return forge_checks.doctor_lines(cfg or _cfg(), object(), gh=gh, path=path)

    def test_a_candidate_host_without_the_declaration_is_red(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _inventory({}, os.path.join(directory, "forge_checks.json"))
            ko, lines = self._doctor(DetailGh(PLAN), path=path)
        self.assertTrue(ko)
        named = "\n".join(lines)
        self.assertIn("KO — nexlink", named)
        self.assertIn("manaty/nexlink", named)
        self.assertIn("develop", named)

    def test_the_versioned_declaration_makes_the_candidate_host_green(self):
        ko, lines = self._doctor(NexlinkPlanGh())
        self.assertFalse(ko)
        self.assertIn("nexlink (manaty/nexlink, develop): versioned declaration installed",
                      "\n".join(lines))

    def test_a_readable_protection_needs_no_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _inventory({}, os.path.join(directory, "forge_checks.json"))
            ko, lines = self._doctor(ReadableGh(), path=path)
        self.assertFalse(ko)
        self.assertIn("protection readable", "\n".join(lines))

    def test_a_rights_403_is_not_conclusive_not_red(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _inventory({}, os.path.join(directory, "forge_checks.json"))
            ko, lines = self._doctor(DetailGh(RIGHTS), path=path)
        self.assertFalse(ko)
        self.assertIn("rights 403", "\n".join(lines))

    def test_a_host_that_is_not_a_candidate_is_not_checked(self):
        ko, lines = self._doctor(DetailGh(PLAN), roles=())
        self.assertFalse(ko)
        self.assertIn("not a candidate host", "\n".join(lines))

    def test_a_host_only_declaration_is_a_warning_not_a_red(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _inventory({}, os.path.join(directory, "forge_checks.json"))
            cfg = _cfg(nexlink={"repo": "/home/ameesh/work/nexlink",
                                "github": "manaty/nexlink", "target": "develop",
                                "required_checks": []})
            ko, lines = self._doctor(NexlinkPlanGh(), cfg=cfg, path=path)
        self.assertFalse(ko)
        self.assertIn("host-only radar declaration", "\n".join(lines))

    def test_an_unreadable_inventory_is_red(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "forge_checks.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{not json")
            ko, lines = self._doctor(DetailGh(PLAN), path=path)
        self.assertTrue(ko)
        self.assertIn("inventory is unreadable", "\n".join(lines))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
