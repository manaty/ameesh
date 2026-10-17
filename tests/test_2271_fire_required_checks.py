# SPDX-License-Identifier: AGPL-3.0-only
"""#2271 : le feu ne tient pas une PR pour verte sans les checks requis.

Constat du 17/10 : la PR #1638 (paquet h-1600, lot #2255) a été fusionnée par
le feu sur la tête gelée 9e790414 alors que le check requis `tests` de `main`
était absent du rollup, qui ne portait qu'une couverture verte et une « suite
complète du paquet » sautée. `fire.ready_pr` ne lisait que `status.ci`, calculé
par `radar.ci_green`, qui compte `SKIPPED` comme vert et ne connaît pas les
checks requis de la branche cible ; `ci_required` n'était appliqué que si la
configuration locale du projet le déclarait.

Le feu lit désormais la protection de la branche cible à la forge, complétée
par la déclaration `radar.projects.<p>.required_checks`, et exige chaque
contexte requis **présent** sur la tête gelée et en `SUCCESS` — un check
absent ou `SKIPPED` laisse la PR non prête. Témoins sans base : rollup du
constat, check sauté, check absent, panne de lecture, déclaration.
"""
from __future__ import annotations

import json
import types
import unittest
from unittest import mock

from ameesh import fire, land, radar

SHA = "9e790414749cdb05e5c446a8e8291f26eaf039e7"
#: le rollup exact de la PR #1638 : couverture verte, suite du paquet sautée,
#: aucun check `tests`.
INCIDENT = [
    {"__typename": "CheckRun", "name": "couverture (PR ouverte sur cette tête)",
     "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"__typename": "CheckRun", "name": "suite complète du paquet",
     "status": "COMPLETED", "conclusion": "SKIPPED"},
]
#: la protection lue à la forge pour `main` le 17/10 : `tests`, app Actions.
PROTECTED = {"required_status_checks": {
    "strict": False, "contexts": ["tests"],
    "checks": [{"context": "tests", "app_id": 15368}]}}


def _check(name, conclusion="SUCCESS", status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion}


def _context(name, state="SUCCESS"):
    return {"__typename": "StatusContext", "context": name, "state": state}


def _status(rollup, **extra):
    row = {"number": 1638, "state": "OPEN", "head": SHA, "base": "main",
           "draft": False, "ci": "green", "merged": False, "merge_commit": "",
           "armed": False, "url": "", "rollup": rollup}
    row.update(extra)
    return row


class FakeGh:
    """Le peu de `gh` que le feu lit : la PR et la protection de la cible."""

    def __init__(self, *, rows=(), protection=PROTECTED, error=""):
        self.rows = list(rows)
        self.protection = protection
        self.error = error
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["pr", "list"]:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(self.rows),
                                         stderr="")
        if argv[:1] == ["api"] and len(argv) > 1 and argv[1].endswith("/protection"):
            if self.error:
                return types.SimpleNamespace(returncode=1, stdout="", stderr=self.error)
            return types.SimpleNamespace(returncode=0,
                                         stdout=json.dumps(self.protection), stderr="")
        return types.SimpleNamespace(returncode=1, stdout="",
                                     stderr="unexpected gh %s" % " ".join(argv))


def _project(**extra):
    return radar.Project("ameesh", "/r", github="manaty/ameesh", **extra)


class ProtectionTest(unittest.TestCase):
    """La source des checks requis : la forge, puis la déclaration du projet."""

    def _required(self, gh, *, project=None, target="main"):
        return fire.required_checks("manaty/ameesh", target, project, gh=gh)

    def test_reads_the_required_contexts_from_the_forge(self):
        gh = FakeGh()
        self.assertEqual(self._required(gh), ("tests",))
        self.assertTrue(any("/protection" in call[1] for call in gh.calls))

    def test_no_protection_is_no_requirement_not_a_read_failure(self):
        gh = FakeGh(error="gh: Branch not protected (HTTP 404)")
        self.assertEqual(self._required(gh), ())

    def test_a_missing_branch_is_not_read_as_no_requirement(self):
        gh = FakeGh(error="gh: Branch not found (HTTP 404)")
        with self.assertRaises(land.LandError):
            self._required(gh)

    def test_an_unreadable_protection_is_an_error_without_a_declaration(self):
        gh = FakeGh(error="gh: Resource not accessible by integration (HTTP 403)")
        with self.assertRaises(land.LandError):
            self._required(gh)

    def test_a_project_declaration_covers_an_unreadable_protection(self):
        gh = FakeGh(error="gh: Resource not accessible by integration (HTTP 403)")
        project = _project(required_checks=("coverage",))
        self.assertEqual(self._required(gh, project=project), ("coverage",))

    def test_the_declaration_completes_the_forge_never_weakens_it(self):
        project = _project(required_checks=("coverage",))
        self.assertEqual(self._required(FakeGh(), project=project),
                         ("tests", "coverage"))

    def test_a_target_is_needed_to_read_the_protection(self):
        with self.assertRaises(land.LandError):
            fire.required_checks("manaty/ameesh", "", None, gh=FakeGh())

    def test_a_branch_name_with_a_slash_is_url_encoded(self):
        gh = FakeGh(protection={"required_status_checks": None})
        self.assertEqual(self._required(gh, target="release/1.11.0"), ())
        self.assertIn("branches/release%2F1.11.0/protection", gh.calls[0][1])

    def test_malformed_protection_is_refused(self):
        for bad in ({"required_status_checks": {"contexts": "tests"}},
                    {"required_status_checks": {"checks": [{"app_id": 1}]}},
                    {"required_status_checks": {"checks": ["tests"]}}):
            with self.subTest(bad=bad), self.assertRaises(land.LandError):
                self._required(FakeGh(protection=bad))


class ReadyPrTest(unittest.TestCase):
    """Le constat, et chaque façon dont un check requis ne prouve pas sa tête."""

    def ready(self, rollup, *, project=None, gh=None, slug="manaty/ameesh",
              required=None):
        return fire.ready_pr(_status(rollup), target="main", project=project,
                             frozen=SHA, required=required, slug=slug,
                             gh=gh if gh is not None else FakeGh())

    def test_the_incident_rollup_is_not_ready(self):
        # Témoin rouge : sans la protection de la forge, ce rollup lisait vert.
        ok, why = self.ready(INCIDENT)
        self.assertFalse(ok)
        self.assertIn("tests (absent)", why)

    def test_a_skipped_required_check_is_not_success(self):
        ok, why = self.ready([_check("tests", "SKIPPED"), _check("couverture")])
        self.assertFalse(ok)
        self.assertIn("tests (SKIPPED)", why)

    def test_a_pending_required_check_is_not_ready(self):
        ok, why = self.ready([_check("tests", None, "IN_PROGRESS")])
        self.assertFalse(ok)
        self.assertIn("tests (PENDING)", why)

    def test_a_successful_required_check_is_ready(self):
        ok, why = self.ready([_check("tests"), _check("suite complète du paquet",
                                                      "SKIPPED")])
        self.assertTrue(ok, why)

    def test_a_required_commit_status_counts(self):
        ok, why = self.ready([_context("tests")])
        self.assertTrue(ok, why)

    def test_a_non_success_duplicate_of_the_required_name_blocks(self):
        ok, why = self.ready([_check("tests"), _check("tests", "SKIPPED")])
        self.assertFalse(ok)
        self.assertIn("tests (SKIPPED)", why)

    def test_a_local_ci_required_off_does_not_weaken_the_forge(self):
        project = _project(ci_required=False)
        ok, _ = self.ready(INCIDENT, project=project)
        self.assertFalse(ok)

    def test_a_declaration_is_enforced_when_the_forge_is_unreadable(self):
        gh = FakeGh(error="gh: Resource not accessible by integration (HTTP 403)")
        project = _project(required_checks=("tests",))
        ok, why = self.ready(INCIDENT, project=project, gh=gh)
        self.assertFalse(ok, why)
        self.assertIn("tests (absent)", why)
        ok, why = self.ready([_check("tests")], project=project, gh=gh)
        self.assertTrue(ok, why)

    def test_without_a_reader_the_gate_fails_closed(self):
        ok, why = fire.ready_pr(_status(INCIDENT), target="main", project=None,
                                frozen=SHA, slug=None, gh=None)
        self.assertFalse(ok)
        self.assertIn("required check", why)

    def test_an_explicit_empty_requirement_keeps_the_old_gate(self):
        ok, why = self.ready(INCIDENT, required=())
        self.assertTrue(ok, why)


class PrStatusTest(unittest.TestCase):
    def test_pr_status_keeps_the_rollup_although_skipped_reads_green(self):
        rows = [{"number": 1638, "state": "OPEN", "headRefOid": SHA,
                 "baseRefName": "main", "isDraft": False,
                 "autoMergeRequest": None, "mergeCommit": None, "url": "",
                 "statusCheckRollup": INCIDENT}]
        gh = FakeGh(rows=rows)
        status = fire._pr_status("manaty/ameesh", "integration/paquet-h", gh=gh)
        self.assertEqual(status["ci"], "green")   # la lecture qui a caché le trou
        self.assertEqual(status["rollup"], INCIDENT)
        ok, _ = fire.ready_pr(status, target="main", project=None, frozen=SHA,
                              slug="manaty/ameesh", gh=gh)
        self.assertFalse(ok)


class SettingsTest(unittest.TestCase):
    def test_declared_required_checks(self):
        settings = radar.parse_settings({"projects": {"ameesh": {
            "repo": "/r", "required_checks": ["tests", "coverage"]}}})
        self.assertEqual(settings.projects["ameesh"].required_checks,
                         ("tests", "coverage"))
        settings = radar.parse_settings({"projects": {"ameesh": {
            "repo": "/r", "required_checks": "tests"}}})
        self.assertEqual(settings.projects["ameesh"].required_checks, ("tests",))

    def test_malformed_required_checks_are_refused(self):
        for bad in ([""], [1], {"tests": 1}, False, 0, [["tests"]]):
            with self.subTest(bad=bad), self.assertRaises(radar.SettingsError):
                radar.parse_settings({"projects": {"ameesh": {
                    "repo": "/r", "required_checks": bad}}})


class WiringTest(unittest.TestCase):
    """Le passage direct donne bien le lecteur à la porte `ready_pr`."""

    ROW = {"id": 42, "title": "un lot", "branch": "lot/42-x", "assignee": "author",
           "app": "ameesh", "branch_target": "main", "state": "build"}

    def test_merge_direct_reads_the_protection_before_merging(self):
        marks = [{"kind": "frozen", "sha": SHA, "actor": "pilote", "at_ts": 0.0, "id": 1},
                 {"kind": "fire", "sha": SHA, "actor": "pilote", "at_ts": 1.0, "id": 2}]
        gh = FakeGh()
        out = {}
        result = fire._merge_direct(None, None, self.ROW, marks, fire.active(marks),
                                    _status(INCIDENT), out=out, slug="manaty/ameesh",
                                    branch="lot/42-x", target="main", info={}, repo=None,
                                    project=None, gh=gh, dry_run=True, handle=None)
        self.assertEqual(result["result"], "not-ready")
        self.assertIn("tests (absent)", result["detail"])
        self.assertTrue(any("/protection" in call[1] for call in gh.calls))


if __name__ == "__main__":
    unittest.main()
