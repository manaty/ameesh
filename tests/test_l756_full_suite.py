# SPDX-License-Identifier: AGPL-3.0-only
"""L756 (#566) : la suite **complète** lue sur un commit exact.

`radar.full_suite_ci` ne retient que les exécutions de la suite **déclarée**
par le projet (`radar.projects.<p>.full_suite`, des fichiers de workflow
résolus en identités), déclenchées par une poussée ou un lancement manuel :
jamais une PR, dont le plan peut avoir été impacté seulement (ou vide, quand
elle ne change qu'une version), jamais un workflow non déclaré (un dispatch
docs-only ne certifie rien), et jamais une exécution dont les jobs sont
`skipped`/`neutral` ou illisibles — le vert se prouve job par job. C'est cette
lecture qui certifie la tête d'un lot de classe release.

Témoins de la R1 auditeur/Codex (PR manaty/ameesh#633 @ dc752238) : B1 (un
dispatch docs-only, puis une suite aux jobs `skipped`, passaient pour verts).
"""
from __future__ import annotations

import shutil
import tempfile
import unittest

from ameesh import plan_github, radar

from .test_l160b_radar import _git
from .test_l160g_nexlink import _Gh

WF = ".github/workflows/tests.yml"
OTHER = ".github/workflows/lint.yml"


def _wf(wid=11, path=WF, state="active"):
    return {"id": wid, "path": path, "state": state}


def _row(wid=11, name="tests", event="push", conclusion="success", status="completed",
         at="2026-10-12T20:00:00Z", sha="a" * 40, run_id=911):
    return {"databaseId": run_id, "workflowDatabaseId": wid, "workflowName": name, "name": name,
            "status": status, "conclusion": conclusion, "event": event, "headSha": sha,
            "createdAt": at, "startedAt": at, "updatedAt": at}


def _job(name="tests", conclusion="success", status="completed"):
    return {"databaseId": 1, "name": name, "status": status, "conclusion": conclusion}


def _gh(rows, jobs=None, workflows=None):
    return _Gh(rows, workflows=[_wf()] if workflows is None else workflows,
               jobs={"911": [_job()]} if jobs is None else jobs)


class FullSuiteReaderTest(unittest.TestCase):
    project = radar.Project("a", "/r", github="o/ameesh", full_suite=(WF,))

    def test_only_a_declared_full_trigger_counts(self):
        # une PR ne compte jamais, même déclarée et verte (plan impacté)
        out = radar.full_suite_ci(self.project, "a" * 40, gh=_gh([_row(event="pull_request")]))
        self.assertEqual((out["runs"], out["green"]), (0, False))
        # un dispatch docs-only (workflow non déclaré) ne compte pas (B1)
        out = radar.full_suite_ci(self.project, "a" * 40,
                                  gh=_gh([_row(wid=99, name="docs", event="workflow_dispatch")]))
        self.assertEqual((out["runs"], out["green"]), (0, False))
        self.assertEqual(out["missing"], [WF])
        # poussée ou lancement manuel de la suite déclarée : vert
        for event in ("push", "workflow_dispatch"):
            with self.subTest(event=event):
                out = radar.full_suite_ci(self.project, "a" * 40, gh=_gh([_row(event=event)]))
                self.assertTrue(out["green"])
                self.assertEqual(out["suites"], 1)

    def test_a_skipped_or_unreadable_job_never_certifies(self):
        # B1 : une exécution verte dont le job de tests est skipped ne certifie rien
        gh = _gh([_row()], jobs={"911": [_job("tests", "skipped"), _job("build", "success")]})
        out = radar.full_suite_ci(self.project, "a" * 40, gh=gh)
        self.assertEqual((out["green"], out["failed"]), (False, ["tests/tests"]))
        gh = _gh([_row()], jobs={"911": [_job("tests", "neutral")]})
        out = radar.full_suite_ci(self.project, "a" * 40, gh=gh)
        self.assertEqual((out["green"], out["failed"]), (False, ["tests/tests"]))
        # un job encore en cours : pas encore vert
        gh = _gh([_row()], jobs={"911": [_job("tests", None, "in_progress")]})
        out = radar.full_suite_ci(self.project, "a" * 40, gh=gh)
        self.assertEqual((out["green"], out["pending"]), (False, ["tests/tests"]))
        # un job illisible (liste vide) ne vaut jamais vert
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(self.project, "a" * 40, gh=_gh([_row()], jobs={}))
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(self.project, "a" * 40,
                                gh=_gh([_row()], jobs={"911": [{"name": "tests"}]}))

    def test_a_missing_declared_suite_is_never_green(self):
        project = radar.Project("a", "/r", github="o/ameesh", full_suite=(WF, OTHER))
        out = radar.full_suite_ci(project, "a" * 40,
                                  gh=_gh([_row()], workflows=[_wf(), _wf(12, OTHER)]))
        self.assertEqual((out["runs"], out["suites"], out["green"]), (1, 2, False))
        self.assertEqual(out["missing"], [OTHER])

    def test_the_declared_suite_must_be_a_workflow_of_the_repository(self):
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(self.project, "a" * 40, gh=_gh([_row()], workflows=[]))
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(self.project, "a" * 40,
                                gh=_gh([_row()], workflows=[_wf(state="disabled_manually")]))
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(radar.Project("a", "/r", github="o/ameesh"), "a" * 40,
                                gh=_gh([_row()]))

    def test_the_latest_full_run_of_each_workflow_counts(self):
        rows = [_row(conclusion="failure", at="2026-10-12T19:00:00Z", run_id=910),
                _row(at="2026-10-12T20:00:00Z", run_id=911)]
        self.assertTrue(radar.full_suite_ci(self.project, "a" * 40, gh=_gh(rows))["green"])
        rows[1]["conclusion"] = "failure"
        out = radar.full_suite_ci(self.project, "a" * 40, gh=_gh(rows))
        self.assertEqual((out["green"], out["failed"]), (False, ["tests"]))

    def test_pending_and_empty_are_reported(self):
        out = radar.full_suite_ci(self.project, "a" * 40,
                                  gh=_gh([_row(status="in_progress", conclusion=None)]))
        self.assertEqual((out["green"], out["pending"]), (False, ["tests"]))
        out = radar.full_suite_ci(self.project, "a" * 40, gh=_gh([]))
        self.assertEqual((out["runs"], out["green"]), (0, False))
        self.assertEqual(out["missing"], [WF])

    def test_an_unreadable_or_wrong_read_is_never_green(self):
        tmp = tempfile.mkdtemp(prefix="ameesh-l756-nogithub-")
        self.addCleanup(shutil.rmtree, tmp, True)
        _git(tmp, "init", "-q")
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(radar.Project("a", tmp), "a" * 40, gh=_Gh([]))
        rows = [_row(sha="b" * 40)]
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(self.project, "a" * 40, gh=_gh(rows))
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(self.project, "a" * 40,
                                gh=_gh([_row(wid=None)]))
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(self.project, "a" * 40,
                                gh=_gh([_row()], workflows=[{"id": None, "path": WF,
                                                             "state": "active"}]))
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(self.project, "a" * 40,
                                gh=_gh([dict(_row(), createdAt=None)]))
        with self.assertRaises(plan_github.GithubError):
            radar.full_suite_ci(self.project, "a" * 40, gh=_gh([_row()] * 100))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
