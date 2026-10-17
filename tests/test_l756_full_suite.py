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

import json
import pathlib
import shutil
import tempfile
import unittest

from ameesh import merge_queue, plan_github, radar

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


#: Fixture brute : `gh run view 38350245246 --repo manaty/ameesh --json jobs`
#: (poussée `release/v1.10.0`, 27 jobs), non retouchée — sa première ligne
#: nomme la commande et le run ; les témoins mutent des copies, jamais le
#: fichier (B3, R2 auditeur de PR manaty/ameesh#633).
FIXTURE = (pathlib.Path(__file__).resolve().parent / "fixtures"
           / "l756-run-38350245246-jobs.txt")


def _release_run_jobs():
    """Les jobs de la fixture brute (mêmes noms d'affichage que l'API)."""
    body = "".join(line for line in FIXTURE.read_text(encoding="utf-8").splitlines(True)
                   if not line.startswith("#"))
    return json.loads(body)["jobs"]


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

    def test_the_job_read_names_the_repository(self):
        # R2 de mty-amh-qa-01 (PR manaty/ameesh#633) : `gh run view` sans
        # `--repo` résout le dépôt par le répertoire courant (`Gh.run` ne fixe
        # ni `cwd` ni `GH_REPO`) et échoue hors d'un clone (« failed to
        # determine base repo »). Le faux `_Gh` ignore les arguments en trop :
        # le témoin exige donc le slug sur l'appel.
        gh = _gh([_row()])
        self.assertTrue(radar.full_suite_ci(self.project, "a" * 40, gh=gh)["green"])
        views = [argv for argv in gh.calls if argv[:2] == ["run", "view"]]
        self.assertEqual(len(views), 1)
        self.assertIn("--repo", views[0])
        self.assertEqual(views[0][views[0].index("--repo") + 1], "o/ameesh")

    def test_a_job_skipped_by_design_does_not_block_a_declared_suite(self):
        # B3 (auditeur, R2 de PR manaty/ameesh#633, 12:58) : la forme réelle de
        # tests.yml en push — « version number » (if: pull_request) et
        # « lecteurs de la doc » (docs-only) sont skipped par conception ; ce
        # sont les jobs requis qui prouvent la suite, pas « tous les jobs ».
        project = radar.Project("a", "/r", github="o/ameesh", full_suite=(WF,),
                                full_suite_required=((WF, ("plan", "psycopg *", "psql *",
                                                           "plateforme macOS", "tests")),),
                                full_suite_tolerated=((WF, ("bilan des impactés",)),))
        jobs = {"911": _release_run_jobs()}
        out = radar.full_suite_ci(project, "a" * 40, gh=_gh([_row()], jobs=jobs))
        self.assertTrue(out["green"], out)
        # la politique « tous les jobs » (entrée réduite au chemin) refuse la
        # même exécution : le B3 est reproduit, puis fermé par la déclaration.
        strict = radar.full_suite_ci(self.project, "a" * 40, gh=_gh([_row()], jobs=jobs))
        self.assertEqual((strict["green"], strict["failed"]),
                         (False, ["tests/lecteurs de la doc", "tests/version number"]))
        # sans astérisque, un requis ne couvre pas ses instances de matrice
        exact = radar.Project("a", "/r", github="o/ameesh", full_suite=(WF,),
                              full_suite_required=((WF, ("plan", "psycopg", "tests")),))
        out = radar.full_suite_ci(exact, "a" * 40, gh=_gh([_row()], jobs=jobs))
        self.assertFalse(out["green"])
        self.assertIn("tests/psycopg (required job missing)", out["failed"])
        # « tests » est exact : les check runs « tests (flux) … » du flux L161c
        # (steps=0) ne le satisfont pas.
        without_tests = [dict(job) for job in jobs["911"] if job["name"] != "tests"]
        out = radar.full_suite_ci(project, "a" * 40,
                                  gh=_gh([_row()], jobs={"911": without_tests}))
        self.assertEqual((out["green"], out["failed"]),
                         (False, ["tests/tests (required job missing)"]))

    def test_a_tolerated_job_neither_certifies_nor_blocks(self):
        # décision B3 (auditeur 12:58) : le « bilan des impactés » est documenté
        # « never required » (EXPLOITATION §L161) et porte
        # `continue-on-error: true` : sa conclusion ne certifie ni ne bloque, et
        # son absence non plus. Un autre job non requis, lui, doit être vert ou
        # sauté — une part de flux L161c rouge refuse la suite.
        project = radar.Project("a", "/r", github="o/ameesh", full_suite=(WF,),
                                full_suite_required=((WF, ("plan",)),),
                                full_suite_tolerated=((WF, ("bilan des impactés",)),))
        jobs = _release_run_jobs()
        bilan_red = [dict(job, conclusion="failure") if job["name"] == "bilan des impactés"
                     else dict(job) for job in jobs]
        self.assertTrue(radar.full_suite_ci(
            project, "a" * 40, gh=_gh([_row()], jobs={"911": bilan_red}))["green"])
        no_bilan = [dict(job) for job in jobs if job["name"] != "bilan des impactés"]
        self.assertTrue(radar.full_suite_ci(
            project, "a" * 40, gh=_gh([_row()], jobs={"911": no_bilan}))["green"])
        flux_red = [dict(job, conclusion="failure")
                    if job["name"] == "tests (flux) psql 1/6" else dict(job) for job in jobs]
        out = radar.full_suite_ci(project, "a" * 40, gh=_gh([_row()], jobs={"911": flux_red}))
        self.assertEqual((out["green"], out["failed"]),
                         (False, ["tests/tests (flux) psql 1/6"]))

    def test_required_jobs_must_exist_and_be_green(self):
        project = radar.Project("a", "/r", github="o/ameesh", full_suite=(WF,),
                                full_suite_required=((WF, ("plan", "tests")),))
        # un job requis sauté ne certifie rien (#876 : skipped vaut success pour
        # un check requis — le piège que la porte refuse)
        out = radar.full_suite_ci(project, "a" * 40, gh=_gh(
            [_row()], jobs={"911": [_job("plan"), _job("tests", "skipped")]}))
        self.assertEqual((out["green"], out["failed"]), (False, ["tests/tests"]))
        # un job requis absent est un refus, jamais un vert
        out = radar.full_suite_ci(project, "a" * 40,
                                  gh=_gh([_row()], jobs={"911": [_job("plan")]}))
        self.assertEqual((out["green"], out["failed"]),
                         (False, ["tests/tests (required job missing)"]))
        # une conclusion nulle (job encore en cours) : jamais vert
        out = radar.full_suite_ci(project, "a" * 40, gh=_gh(
            [_row()], jobs={"911": [_job("plan"), _job("tests", None, "in_progress")]}))
        self.assertEqual((out["green"], out["pending"]), (False, ["tests/tests"]))
        # un nom qui ne fait que commencer pareil ne compte pas (« tests-extra »)
        out = radar.full_suite_ci(project, "a" * 40, gh=_gh(
            [_row()], jobs={"911": [_job("plan"), _job("tests-extra")]}))
        self.assertEqual((out["green"], out["failed"]),
                         (False, ["tests/tests (required job missing)"]))
        # une seule part de matrice rouge refuse la suite (« psycopg * »)
        matrix_project = radar.Project("a", "/r", github="o/ameesh", full_suite=(WF,),
                                       full_suite_required=((WF, ("plan", "psycopg *")),))
        matrix = [dict(job, conclusion="failure") if job["name"] == "psycopg 2/4"
                  else dict(job) for job in _release_run_jobs()]
        out = radar.full_suite_ci(matrix_project, "a" * 40,
                                  gh=_gh([_row()], jobs={"911": matrix}))
        self.assertEqual((out["green"], out["failed"]), (False, ["tests/psycopg 2/4"]))
        # une famille sans aucune correspondance est un requis absent, donc rouge
        no_matrix = [dict(job) for job in _release_run_jobs()
                     if not job["name"].startswith("psycopg ")]
        out = radar.full_suite_ci(matrix_project, "a" * 40,
                                  gh=_gh([_row()], jobs={"911": no_matrix}))
        self.assertEqual((out["green"], out["failed"]),
                         (False, ["tests/psycopg * (required job missing)"]))

    def test_a_non_required_job_is_success_or_skipped_only(self):
        project = radar.Project("a", "/r", github="o/ameesh", full_suite=(WF,),
                                full_suite_required=((WF, ("plan",)),))
        base = [_job("plan")]
        out = radar.full_suite_ci(project, "a" * 40, gh=_gh(
            [_row()], jobs={"911": base + [_job("lint", "neutral")]}))
        self.assertEqual((out["green"], out["failed"]), (False, ["tests/lint"]))
        out = radar.full_suite_ci(project, "a" * 40, gh=_gh(
            [_row()], jobs={"911": base + [_job("lint", "skipped")]}))
        self.assertTrue(out["green"], out)
        out = radar.full_suite_ci(project, "a" * 40, gh=_gh(
            [_row()], jobs={"911": base + [_job("lint", None, "in_progress")]}))
        self.assertEqual((out["green"], out["pending"]), (False, ["tests/lint"]))

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


class RequiredFullCiTest(unittest.TestCase):
    """#886 (P3, R2 mty-amh-qa-01 sur PR manaty/ameesh#633) : la porte de la
    suite complète ne prend pas une **déclaration manquante** pour une panne
    gh — le message nomme `radar.projects.<p>.full_suite` et ne renvoie pas à
    « once gh works », qui masquait la configuration à réparer."""

    def test_a_missing_declaration_is_named_without_gh(self):
        project = radar.Project("a", "/r", github="o/ameesh")  # aucune full_suite
        with self.assertRaises(merge_queue.QueueError) as caught:
            merge_queue.required_full_ci(project, "a" * 40, lot=886)
        text = str(caught.exception)
        self.assertIn("radar.projects.a.full_suite", text)
        self.assertNotIn("once gh works", text)

    def test_an_unreadable_declared_suite_still_names_gh(self):
        # contraste : une suite déclarée mais illisible garde le message de
        # lecture impossible — le repli « once gh works » n'est pas perdu.
        project = radar.Project("a", "/r", github="o/ameesh", full_suite=(WF,))

        def boom(sha):
            raise plan_github.GithubError("gh down")

        with self.assertRaises(merge_queue.QueueError) as caught:
            merge_queue.required_full_ci(project, "a" * 40, full_ci=boom, lot=886)
        text = str(caught.exception)
        self.assertIn("cannot read the full-suite", text)
        self.assertIn("once gh works", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
