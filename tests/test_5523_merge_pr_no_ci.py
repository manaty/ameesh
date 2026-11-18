"""#5523 : un dépôt sans CI (canon `manaty/home`) ne pouvait jamais passer
`merge-pr` — `radar.ci_green` refuse tout rollup vide, même quand le projet
déclare `required_checks: []`.

Le rollup vide vaut vert seulement si la déclaration est vide **et** que la
forge prouve l'absence de toute CI (aucun workflow, aucun run, check run ni
statut sur la tête). Toute autre absence garde le refus."""

from __future__ import annotations

import json
import types
import unittest

from ameesh import merge_pr, radar

HEAD = "8d2b92b05cc1" + "0" * 28


def _project(**extra):
    return radar.Project("home", "/r", github="manaty/home", **extra)


EMPTY_DECLARED = dict(required_checks=(), required_checks_declared=True)


class ForgeGh:
    """Les lectures de `_no_ci_proven` : workflows, runs, check runs et
    statuts de la tête ; une forge sans CI par défaut."""

    def __init__(self, *, workflows=(), runs=(), check_runs=(), statuses=(),
                 error=""):
        self.workflows = list(workflows)
        self.runs = list(runs)
        self.check_runs = list(check_runs)
        self.statuses = list(statuses)
        self.error = error
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        path = argv[-1]
        if self.error:
            return types.SimpleNamespace(returncode=1, stdout="", stderr=self.error)
        if "/actions/workflows" in path:
            page = {"total_count": len(self.workflows), "workflows": self.workflows}
        elif "/actions/runs" in path:
            page = {"total_count": len(self.runs), "workflow_runs": self.runs}
        elif "/check-runs" in path:
            page = {"total_count": len(self.check_runs), "check_runs": self.check_runs}
        elif "/statuses" in path:
            page = self.statuses
        else:
            return types.SimpleNamespace(returncode=1, stdout="",
                                         stderr="unexpected gh %s" % " ".join(argv))
        return types.SimpleNamespace(returncode=0, stdout=json.dumps([page]), stderr="")


def _view(rollup=(), **over):
    data = {"number": 249, "state": "OPEN", "isDraft": False,
            "baseRefName": "main", "headRefOid": HEAD,
            "headRefName": "canon/5523", "mergeStateStatus": "CLEAN",
            "statusCheckRollup": list(rollup)}
    data.update(over)
    return data


class NoCiRepoTest(unittest.TestCase):

    def guards(self, project, gh, rollup=()):
        merge_pr._pr_guards(_view(rollup), 249, HEAD, "main", project,
                            slug="manaty/home", gh=gh)

    def assertRefused(self, project, gh, rollup=()):
        with self.assertRaises(merge_pr.MergePrError) as raised:
            self.guards(project, gh, rollup)
        self.assertIn("not all concluded and green", str(raised.exception))

    def test_empty_declaration_and_a_forge_without_ci_is_green(self):
        gh = ForgeGh()
        self.guards(_project(**EMPTY_DECLARED), gh)
        read = " ".join(arg for call in gh.calls for arg in call)
        for source in ("/actions/workflows", "/actions/runs?head_sha=" + HEAD,
                       "/commits/%s/check-runs" % HEAD, "/commits/%s/statuses" % HEAD):
            self.assertIn(source, read)

    def test_an_empty_rollup_without_declaration_is_refused(self):
        self.assertRefused(_project(), ForgeGh())

    def test_a_workflow_present_keeps_the_refusal(self):
        # CI pas encore démarrée : le dépôt a un workflow, la tête rien encore
        gh = ForgeGh(workflows=[{"id": 1, "path": ".github/workflows/ci.yml"}])
        self.assertRefused(_project(**EMPTY_DECLARED), gh)

    def test_a_run_check_run_or_status_on_the_head_keeps_the_refusal(self):
        for gh in (ForgeGh(runs=[{"id": 3, "status": "queued"}]),
                   ForgeGh(check_runs=[{"name": "tests"}]),
                   ForgeGh(statuses=[{"context": "ci", "state": "pending"}])):
            self.assertRefused(_project(**EMPTY_DECLARED), gh)

    def test_an_unreadable_forge_keeps_the_refusal(self):
        self.assertRefused(_project(**EMPTY_DECLARED),
                           ForgeGh(error="gh: Bad credentials (HTTP 401)"))
        with self.assertRaises(merge_pr.MergePrError):
            merge_pr._pr_guards(_view(), 249, HEAD, "main",
                                _project(**EMPTY_DECLARED))  # sans gh ni slug

    def test_a_declared_check_or_status_keeps_the_refusal(self):
        self.assertRefused(_project(required_checks=("tests",),
                                    required_checks_declared=True), ForgeGh())
        self.assertRefused(_project(required_statuses=(("*", "paquet"),),
                                    **EMPTY_DECLARED), ForgeGh())

    def test_a_non_empty_rollup_is_judged_by_ci_green_alone(self):
        failing = [{"__typename": "CheckRun", "name": "x", "status": "COMPLETED",
                    "conclusion": "FAILURE"}]
        self.assertRefused(_project(**EMPTY_DECLARED), ForgeGh(), failing)


if __name__ == "__main__":
    unittest.main()
