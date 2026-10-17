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
absent ou `SKIPPED` laisse la PR non prête. La protection peut imposer une
application GitHub (`checks[].app_id`) : le feu lit alors la provenance des
check runs de la tête exacte et refuse un homonyme d'une autre application,
un statut de commit sans source, ou une source illisible (B1 de la relecture
du 17/10). Un contexte est un nom exact : ses espaces significatifs sont
conservés de la protection ou de la déclaration jusqu'aux check runs, `tests `
ne vaut pas `tests` (B2 de la relecture, 17/10). Témoins sans base : rollup du
constat, check sauté, check absent, panne de lecture, déclaration, mauvais
fournisseur, noms exacts.
"""
from __future__ import annotations

import json
import types
import unittest

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


def _run(name, app_id=15368, conclusion="success", status="completed", sha=SHA):
    """Un check run de la tête, tel que le lit `commits/<sha>/check-runs`."""
    return {"name": name, "status": status, "conclusion": conclusion,
            "head_sha": sha, "app": {"id": app_id}}


class FakeGh:
    """Le peu de `gh` que le feu lit : la PR, la protection de la cible et les
    check runs de la tête (leur source)."""

    def __init__(self, *, rows=(), protection=PROTECTED, error="", check_runs=(),
                 check_error=""):
        self.rows = list(rows)
        self.protection = protection
        self.error = error
        self.check_runs = list(check_runs)
        self.check_error = check_error
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
        path = next((arg for arg in argv if "/check-runs" in arg), "")
        if path:
            if self.check_error:
                return types.SimpleNamespace(returncode=1, stdout="",
                                             stderr=self.check_error)
            page = {"total_count": len(self.check_runs),
                    "check_runs": self.check_runs}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps([page]),
                                         stderr="")
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
        self.assertEqual(self._required(gh), (("tests", 15368),))
        self.assertTrue(any("/protection" in call[1] for call in gh.calls))

    def test_the_protection_keeps_the_imposed_app(self):
        # B1 : `main` impose `tests` à l'application Actions (15368) ; réduire
        # le check à son nom accepterait un homonyme de n'importe qui.
        self.assertEqual(fire._protection_required("manaty/ameesh", "main", gh=FakeGh()),
                         (("tests", 15368),))

    def test_the_protection_keeps_a_context_name_exactly(self):
        # B2 : un nom de contexte est un nom exact ; `tests ` n'est pas
        # `tests`, et la protection doit le rendre tel quel.
        gh = FakeGh(protection={"required_status_checks": {
            "contexts": ["tests "],
            "checks": [{"context": "tests ", "app_id": 15368}]}})
        self.assertEqual(self._required(gh), (("tests ", 15368),))

    def test_a_legacy_context_imposes_no_app(self):
        gh = FakeGh(protection={"required_status_checks": {
            "contexts": ["tests"], "checks": []}})
        self.assertEqual(self._required(gh), (("tests", None),))

    def test_an_app_scoped_check_is_not_duplicated_as_an_unscoped_context(self):
        # La forge renvoie `contexts` et `checks` pour la même exigence : le
        # doublon sans application n'ajoute aucune garantie.
        gh = FakeGh(protection={"required_status_checks": {
            "contexts": ["tests", "coverage"],
            "checks": [{"context": "tests", "app_id": 15368}]}})
        self.assertEqual(self._required(gh), (("tests", 15368), ("coverage", None)))

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
        self.assertEqual(self._required(gh, project=project), (("coverage", None),))

    def test_the_declaration_completes_the_forge_never_weakens_it(self):
        project = _project(required_checks=("coverage",))
        self.assertEqual(self._required(FakeGh(), project=project),
                         (("tests", 15368), ("coverage", None)))

    def test_a_declared_context_keeps_its_significant_spaces(self):
        # B2 : la déclaration traverse jusqu'aux paires sans être rognée.
        gh = FakeGh(protection={"required_status_checks": None})
        project = _project(required_checks=("tests ",))
        self.assertEqual(self._required(gh, project=project), (("tests ", None),))

    def test_a_declared_context_that_differs_by_a_space_is_not_merged(self):
        project = _project(required_checks=("tests ",))
        self.assertEqual(self._required(FakeGh(), project=project),
                         (("tests", 15368), ("tests ", None)))

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
                    {"required_status_checks": {"checks": ["tests"]}},
                    {"required_status_checks": {"checks": [
                        {"context": "tests", "app_id": "15368"}]}},
                    {"required_status_checks": {"checks": [
                        {"context": "tests", "app_id": True}]}}):
            with self.subTest(bad=bad), self.assertRaises(land.LandError):
                self._required(FakeGh(protection=bad))


class ReadyPrTest(unittest.TestCase):
    """Le constat, et chaque façon dont un check requis ne prouve pas sa tête."""

    def ready(self, rollup, *, project=None, gh=None, slug="manaty/ameesh",
              required=None, frozen=SHA):
        return fire.ready_pr(_status(rollup), target="main", project=project,
                             frozen=frozen, required=required, slug=slug,
                             gh=gh if gh is not None else FakeGh())

    def test_the_incident_rollup_is_not_ready(self):
        # Témoin rouge : sans la protection de la forge, ce rollup lisait vert.
        ok, why = self.ready(INCIDENT)
        self.assertFalse(ok)
        self.assertIn("tests (absent)", why)

    def test_a_skipped_required_check_is_not_success(self):
        gh = FakeGh(check_runs=[_run("tests", conclusion="skipped")])
        ok, why = self.ready([_check("tests", "SKIPPED"), _check("couverture")], gh=gh)
        self.assertFalse(ok)
        self.assertIn("tests (SKIPPED)", why)

    def test_a_pending_required_check_is_not_ready(self):
        gh = FakeGh(check_runs=[_run("tests", conclusion=None, status="in_progress")])
        ok, why = self.ready([_check("tests", None, "IN_PROGRESS")], gh=gh)
        self.assertFalse(ok)
        self.assertIn("tests (PENDING)", why)

    def test_a_successful_required_check_is_ready(self):
        gh = FakeGh(check_runs=[_run("tests")])
        ok, why = self.ready([_check("tests"), _check("suite complète du paquet",
                                                      "SKIPPED")], gh=gh)
        self.assertTrue(ok, why)

    def test_a_required_check_from_the_expected_app_is_ready(self):
        # Le check run de la tête vient de l'application 15368 qu'impose la
        # protection : la porte est franchie.
        gh = FakeGh(check_runs=[_run("tests", app_id=15368)])
        ok, why = self.ready([_check("tests")], gh=gh)
        self.assertTrue(ok, why)

    def test_a_check_run_name_with_a_trailing_space_does_not_prove_the_context(self):
        # B2, témoin rouge : le check run `tests ` (même application, SUCCESS)
        # satisfaisait à tort le contexte exact `tests`.
        gh = FakeGh(check_runs=[_run("tests ")])
        ok, why = self.ready([_check("tests ")], gh=gh)
        self.assertFalse(ok, why)
        self.assertIn("tests (absent)", why)

    def test_a_check_run_without_the_required_space_does_not_prove_it(self):
        # B2, sens inverse : `tests ` exigé n'est pas prouvé par `tests`.
        gh = FakeGh(protection={"required_status_checks": {
            "contexts": ["tests "],
            "checks": [{"context": "tests ", "app_id": 15368}]}},
            check_runs=[_run("tests")])
        ok, why = self.ready([_check("tests")], gh=gh)
        self.assertFalse(ok, why)
        self.assertIn("(absent)", why)

    def test_a_declared_name_with_a_space_matches_only_its_exact_check(self):
        # B2 de bout en bout : déclaration -> paires -> rollup, sans rognage.
        gh = FakeGh(error="gh: Resource not accessible by integration (HTTP 403)")
        project = _project(required_checks=("tests ",))
        ok, why = self.ready([_check("tests")], project=project, gh=gh)
        self.assertFalse(ok, why)
        ok, why = self.ready([_check("tests ")], project=project, gh=gh)
        self.assertTrue(ok, why)

    def test_an_explicit_required_name_keeps_its_spaces(self):
        gh = FakeGh()
        ok, why = self.ready([_check("tests")], required=("tests ",), gh=gh)
        self.assertFalse(ok, why)
        ok, why = self.ready([_check("tests ")], required=("tests ",), gh=gh)
        self.assertTrue(ok, why)

    def test_a_required_check_from_another_app_is_not_ready(self):
        # B1 : même nom, même `SUCCESS`, mais produit par une autre
        # application : le fournisseur imposé n'est pas prouvé.
        gh = FakeGh(check_runs=[_run("tests", app_id=99999)])
        ok, why = self.ready([_check("tests")], gh=gh)
        self.assertFalse(ok)
        self.assertIn("tests (app 99999, expected 15368)", why)

    def test_a_commit_status_cannot_prove_an_imposed_app(self):
        # B1 : un statut de commit `tests` vert n'a pas de source
        # d'application et ne satisfait pas une protection qui en impose une.
        gh = FakeGh(check_runs=[])
        ok, why = self.ready([_context("tests")], gh=gh)
        self.assertFalse(ok)
        self.assertIn("tests (source absent)", why)

    def test_a_check_run_without_an_app_cannot_prove_the_imposed_app(self):
        gh = FakeGh(check_runs=[{"name": "tests", "status": "completed",
                                 "conclusion": "success", "head_sha": SHA,
                                 "app": None}])
        ok, why = self.ready([_check("tests")], gh=gh)
        self.assertFalse(ok)
        self.assertIn("tests (source absent)", why)

    def test_an_unreadable_check_source_fails_closed(self):
        gh = FakeGh(check_error="gh: Resource not accessible by integration (HTTP 403)")
        ok, why = self.ready([_check("tests")], gh=gh)
        self.assertFalse(ok)
        self.assertIn("cannot be proven", why)

    def test_a_required_commit_status_counts_without_an_imposed_app(self):
        gh = FakeGh(protection={"required_status_checks": {
            "contexts": ["tests"], "checks": []}})
        ok, why = self.ready([_context("tests")], gh=gh)
        self.assertTrue(ok, why)

    def test_a_non_success_duplicate_of_the_required_name_blocks(self):
        gh = FakeGh(check_runs=[_run("tests"), _run("tests", conclusion="skipped")])
        ok, why = self.ready([_check("tests"), _check("tests", "SKIPPED")], gh=gh)
        self.assertFalse(ok)
        self.assertIn("tests (SKIPPED)", why)

    def test_a_pr_head_past_the_freeze_is_not_ready(self):
        ok, why = self.ready([_check("tests")],
                             gh=FakeGh(check_runs=[_run("tests")]),
                             frozen="b" * 40)
        self.assertFalse(ok)
        self.assertIn("not the freeze", why)

    def test_an_abbreviated_freeze_names_the_same_head(self):
        # Un gel peut citer `48c6372e` : la tête complète de la PR le nomme.
        gh = FakeGh(check_runs=[_run("tests")])
        ok, why = self.ready([_check("tests")], gh=gh, frozen=SHA[:8])
        self.assertTrue(ok, why)
        paths = [" ".join(call) for call in gh.calls if any("/check-runs" in a for a in call)]
        self.assertIn("/commits/%s/check-runs" % SHA, paths[0])

    def test_the_checks_are_read_on_the_frozen_head(self):
        gh = FakeGh(check_runs=[_run("tests")])
        self.ready([_check("tests")], gh=gh)
        paths = [call for call in gh.calls if any("/check-runs" in arg for arg in call)]
        self.assertEqual(len(paths), 1)
        self.assertIn("/commits/%s/check-runs" % SHA, " ".join(paths[0]))

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

    def test_an_explicit_name_still_accepts_any_provider(self):
        # L'appelant qui passe des noms (pas de couple) n'impose pas
        # d'application : le rollup suffit, sans lecture de provenance.
        gh = FakeGh()
        ok, why = self.ready([_check("tests")], required=("tests",), gh=gh)
        self.assertTrue(ok, why)
        self.assertFalse(any("/check-runs" in arg for call in gh.calls for arg in call))


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


class PagesGh:
    """`gh api --paginate --slurp` : une liste de pages de check runs."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(self.pages), stderr="")


class HeadCheckRunsTest(unittest.TestCase):
    """La provenance des checks se lit sur la tête exacte, toutes pages."""

    def test_all_pages_of_check_runs_are_read(self):
        gh = PagesGh([{"check_runs": [_run("couverture")]},
                      {"check_runs": [_run("tests")]}])
        runs = fire._head_check_runs("manaty/ameesh", SHA, gh=gh)
        self.assertEqual([run["name"] for run in runs], ["couverture", "tests"])
        self.assertEqual(runs[1]["app_id"], 15368)
        self.assertIn("--paginate", gh.calls[0])
        self.assertIn("--slurp", gh.calls[0])

    def test_an_abbreviated_head_is_accepted(self):
        gh = PagesGh([{"check_runs": [_run("tests")]}])
        runs = fire._head_check_runs("manaty/ameesh", SHA[:8], gh=gh)
        self.assertEqual([run["name"] for run in runs], ["tests"])

    def test_check_run_names_are_kept_exactly(self):
        # B2 : la source de provenance ne rogne pas le nom du check run.
        gh = PagesGh([{"check_runs": [_run("tests ")]}])
        runs = fire._head_check_runs("manaty/ameesh", SHA, gh=gh)
        self.assertEqual([run["name"] for run in runs], ["tests "])

    def test_a_malformed_check_run_is_refused(self):
        for bad in ([{"check_runs": [{"name": "tests", "app": {"id": "15368"}}]}],
                    [{"check_runs": [{"status": "completed"}]}],
                    [{"total_count": 1}]):
            with self.subTest(bad=bad), self.assertRaises(land.LandError):
                fire._head_check_runs("manaty/ameesh", SHA, gh=PagesGh(bad))

    def test_a_check_run_of_another_head_is_refused(self):
        gh = PagesGh([{"check_runs": [_run("tests", sha="b" * 40)]}])
        with self.assertRaises(land.LandError):
            fire._head_check_runs("manaty/ameesh", SHA, gh=gh)

    def test_a_missing_reader_fails_closed(self):
        for slug, sha, gh in (("", SHA, PagesGh([])),
                              ("manaty/ameesh", SHA, None),
                              ("manaty/ameesh", "", PagesGh([]))):
            with self.subTest(slug=slug, sha=sha), self.assertRaises(land.LandError):
                fire._head_check_runs(slug, sha, gh=gh)


class SettingsTest(unittest.TestCase):
    def test_declared_names_keep_their_significant_spaces(self):
        # B2 : ` tests ` n'est pas `tests`, de la configuration au feu.
        settings = radar.parse_settings({"projects": {"ameesh": {
            "repo": "/r", "required_checks": [" tests "]}}})
        project = settings.projects["ameesh"]
        self.assertEqual(project.required_checks, (" tests ",))
        self.assertEqual(fire.required_checks(
            "manaty/ameesh", "main", project,
            gh=FakeGh(protection={"required_status_checks": None})),
            ((" tests ", None),))

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
